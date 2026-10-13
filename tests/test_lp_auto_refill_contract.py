"""Issue #249: public auto-buy contracts with the real adapter and SQLite."""

from tests.test_lp_account_reservation_reconciliation import advance_api_wait
from datetime import timedelta
from decimal import Decimal

import pytest
from polymarket.models.clob import SignedOrder
from polymarket.models.clob.order_book import OrderBookLevel

from tests.test_lp_account_reservation_reconciliation import (
    runtime, _FiveMarketPublic, _refill_identity, _advance,
)
from tests.test_lp_order_registration_contract import WALLET, _open_order


class RefillPublic(_FiveMarketPublic):
    def __init__(self, clock):
        super().__init__(clock)
        self.sizes = {}
        self.rates = {}
        self.prices = {}

    def get_market(self, *, id):
        market = super().get_market(id=id)
        size = Decimal(self.sizes.get(int(id.removeprefix('market-')), '20'))
        return market.model_copy(update={
            'trading': market.trading.model_copy(update={'minimum_order_size': size}),
            'rewards': market.rewards.model_copy(update={'rewards_min_size': size}),
        })

    def list_market_rewards(self, *, condition_id, sponsored):
        reward = super().list_market_rewards(condition_id=condition_id, sponsored=sponsored)[0]
        index = next(i for i in range(1, 7) if _refill_identity(i)[1] == condition_id)
        config = reward.rewards_config[0].model_copy(update={'id': index, 'rate_per_day': Decimal(self.rates.get(index, '24'))})
        return (reward.model_copy(update={'rewards_min_size': Decimal(self.sizes.get(index, '20')),
                                         'rewards_config': (config,)}),)

    def list_current_rewards(self, *, sponsored):
        return tuple(self.list_market_rewards(condition_id=_refill_identity(i)[1], sponsored=sponsored)[0]
                     for i in range(1, 7)) if not sponsored else ()

    def get_order_book(self, *, token_id):
        book = super().get_order_book(token_id=token_id)
        index = next(i for i in range(1, 7) if _refill_identity(i)[2] == token_id or f'0x{i + 300:064x}' == token_id)
        price = Decimal(self.prices.get(index, '.40'))
        return book.model_copy(update={
            'bids': (OrderBookLevel(price=price, size=Decimal('1000')),
                     OrderBookLevel(price=price - Decimal('.01'), size=Decimal('1000'))),
            'asks': (OrderBookLevel(price=price + Decimal('.02'), size=Decimal('1000')),),
        })


def prepare(runtime, *, budget='100', target=5, count=5, public=None):
    public = public or RefillPublic(runtime.clock)
    store, adapter, account, lp, execution = runtime(public_client=public)
    execution.lp_auto_configure({'budget_usd': budget, 'target_buy_count': target})
    for i in range(1, count + 1):
        market, condition, token = _refill_identity(i)
        facts = lp._read_candidate_facts({'market_id': market, 'condition_id': condition, 'token_id': token, 'outcome': 'YES'})
        lp._candidate_pool_record_success(condition, {'condition_id': condition}, judged_at=lp._now(),
                                         facts={'directions': [facts['direction']], 'account': facts['account']})
        store.lp_save_price_history(condition, token, [], {'state': 'known', 'amplitude': Decimal('.005'),
            'checked_at': lp._now(), 'valid_until': lp._now() + timedelta(days=1)})

    def sign(**kwargs):
        _advance(runtime)
        return SignedOrder(builder='0x1', expiration=kwargs['expiration'], maker=WALLET,
            maker_amount=int(kwargs['price'] * kwargs['size'] * 1000000), metadata='0x3',
            order_type='GTD', salt=1, side='BUY', signature='0x4', signature_type=0,
            signer=WALLET, taker_amount=int(kwargs['size'] * 1000000), timestamp=1,
            token_id=str(kwargs['token_id']), post_only=True)

    def post(signed):
        _advance(runtime)
        account.posts.append(signed)
        index = next(i for i in range(1, 7) if signed.token_id in {_refill_identity(i)[2], f'0x{i + 300:064x}'})
        condition = _refill_identity(index)[1]
        price = Decimal(signed.maker_amount) / signed.taker_amount
        oid = f'refill-{index}'
        order = _open_order(oid, 'BUY', price=str(price), original=str(Decimal(signed.taker_amount) / 1000000),
            token_id=signed.token_id, outcome='YES' if signed.token_id == _refill_identity(index)[2] else 'NO').model_copy(update={'market': condition, 'condition_id': condition})
        account.orders += (order,)
        return {'order_id': oid, 'status': 'LIVE', 'accepted': True, 'size_matched': '0'}

    balance_reader = account.get_balance_allowance
    def current_balance(**kwargs):
        _advance(runtime)
        return balance_reader(**kwargs)
    account.get_balance_allowance = current_balance
    account.create_limit_order = sign
    account.post_order = post
    execution.lp_auto_set_desired_running(True)
    _advance(runtime)
    return store, adapter, account, lp, execution, public


def test_current_account_replaces_stale_candidate_account(runtime):
    _, _, account, lp, execution, _ = prepare(runtime, count=2, target=2)
    for cached in lp._candidate_qualification_facts.values():
        cached['account']['checked_at'] = lp._now() - timedelta(seconds=61)
    planning_as_of, financial_calls = [], []
    sign, balance = account.create_limit_order, account.get_balance_allowance
    def observed_sign(**kwargs):
        planning_as_of.append(execution.lp_auto_state()['funds']['as_of'])
        return sign(**kwargs)
    def observed_balance(**kwargs):
        financial_calls.append(bool(execution.lp_auto_state()['active_plan']))
        return balance(**kwargs)
    account.create_limit_order, account.get_balance_allowance = observed_sign, observed_balance
    state = execution.lp_auto_run_once(round_id='stale-candidate-account')
    assert len(account.posts) == 2
    assert state['slots']['occupied'] == 2
    assert planning_as_of and planning_as_of[-1]
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == planning_as_of[-1]
    assert state['funds']['spendable_usd'] is None and state['funds']['available_usd'] is None
    assert not any(financial_calls), 'Approved plan must not read balance/allowance'
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = execution.lp_auto_state()
    assert state['funds']['status'] == 'known'


@pytest.mark.parametrize('change', ['rank', 'price-same-rank'])
def test_presend_relative_rank_change_skips_only_this_candidate(runtime, change):
    from collections import Counter

    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24', 3: '12'}
    _, _, account, lp, execution, _ = prepare(runtime, count=3, target=2, public=public)
    # Change external market facts after qualification, before preparation.
    if change == 'rank':
        public.rates[1] = '6'
    else:
        public.prices[1] = '.39'
    state = execution.lp_auto_run_once(round_id='relative-rank')
    tokens = [p.token_id for p in account.posts]
    assert Counter(tokens) == Counter(_refill_identity(i)[2] for i in ([2, 3] if change == 'rank' else [1, 2])), state['last_round']
    if change == 'rank':
        assert [r['token_id'] for r in state['last_round']['targets']] == [_refill_identity(i)[2] for i in [2, 3]]
        assert _refill_identity(1)[2] not in tokens
    else:
        intended, = [p for p in account.posts if p.token_id == _refill_identity(1)[2]]
        assert intended.maker_amount == 7800000
    assert state['slots']['occupied'] == 2
    assert Decimal(state['funds']['buy_reserved_usd']) <= 16


@pytest.mark.parametrize('phase', ['preparation', 'signing'])
@pytest.mark.parametrize('invalid, reason', [
    ('unknown', 'history_summary_unknown'),
    ('expired', 'history_summary_expired'),
    ('amplitude', 'history_amplitude_exceeded'),
    ('identity', 'history_identity_mismatch'),
])
def test_selected_price_history_rejects_only_this_candidate(runtime, phase, invalid, reason):
    from copy import deepcopy
    from datetime import datetime
    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24'}
    store, _, account, lp, execution, _ = prepare(runtime, count=2, target=1, public=public)
    _, condition, token = _refill_identity(1)
    # Match a normally published selected-token row for the UI seam.
    lp._candidate_pool_record_success(condition, {'condition_id': condition,
        'selected_direction': {'token_id': token}}, judged_at=lp._now())
    def invalidate():
        summary = {'state': 'known', 'amplitude': Decimal('.005'), 'checked_at': lp._now(),
                   'valid_until': lp._now() + timedelta(days=1)}
        if invalid == 'unknown':
            summary['state'] = 'unknown'
        elif invalid == 'expired':
            summary['valid_until'] = lp._now() - timedelta(seconds=1)
        elif invalid == 'amplitude':
            summary['amplitude'] = Decimal('.02')
        else:
            summary['token_id'] = _refill_identity(2)[2]
        store.lp_save_price_history(condition, token, [], summary)
    planning_as_of, captured, signed_objects = [], [], []
    original_sign = account.create_limit_order
    def sign(**kwargs):
        approved = execution.lp_auto_state()
        planning_as_of.append(approved['funds']['as_of'])
        captured.append(deepcopy(approved))
        signed = original_sign(**kwargs)
        signed_objects.append(signed)
        if phase == 'signing' and kwargs['token_id'] == token:
            invalidate()
        return signed
    account.create_limit_order = sign
    if phase == 'preparation':
        invalidate()
        assert lp.candidate_snapshot()['candidate_valid_count'] == 1
    state = execution.lp_auto_run_once(round_id=f'price-history-{phase}-{invalid}')
    if phase == 'preparation':
        assert [p.token_id for p in account.posts] == [_refill_identity(2)[2]], state['last_round']
        assert state['last_round']['candidate_filter']['recheck_reasons'] == {reason: 1}
        assert len(state['last_round']['actions']) == 1
        assert state['last_round']['actions'][0]['token_id'] == _refill_identity(2)[2]
        assert state['last_round']['actions'][0]['state'] == 'success'
    else:
        assert [p.token_id for p in account.posts] == [token], state['last_round']
        action, = state['last_round']['actions']
        assert action['token_id'] == token and action['state'] == 'success'
        assert action['request_state'] == 'entry_open'
    original = deepcopy(state['last_round'])
    assert len(captured) == len(signed_objects) == 1 and captured[0]['active_plan']
    assert original['round_id'] == captured[0]['last_round']['round_id']
    assert original['targets'] == captured[0]['last_round']['targets']
    assert [a['action_id'] for a in original['actions']] == [a['action_id'] for a in captured[0]['active_plan']['actions']]
    signed, = signed_objects
    assert account.posts == [signed] and isinstance(signed, SignedOrder)
    assert signed.maker_amount == 8000000 and signed.taker_amount == 20000000 and signed.post_only is True
    action, = original['actions']
    target = original['targets'][action['index']]
    assert target['token_id'] == action['token_id'] and target['condition_id'] == action['condition_id']
    assert Decimal(target['price']) == Decimal('.40') and Decimal(target['quantity']) == 20
    audit, = store.lp_actions(action['session_id'])
    assert audit['state'] == 'accepted' and audit['post_started'] is True
    assert audit['token_id'] == action['token_id'] and audit['condition_id'] == action['condition_id']
    assert Decimal(audit['price']) == Decimal('.40') and Decimal(audit['quantity']) == 20
    original_audits = deepcopy(store.lp_actions(action['session_id']))
    assert state['slots']['occupied'] == 1 and Decimal(state['funds']['buy_reserved_usd']) == 8
    assert planning_as_of and planning_as_of[0]
    assert datetime.fromisoformat(planning_as_of[0]) <= runtime.clock[0]
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == planning_as_of[0]
    assert state['funds']['available_usd'] is None and state['funds']['spendable_usd'] is None
    assert 'account_financial_facts_changed' in state['admission_block_reasons']
    assert original['completed_at'] and state['active_plan'] is None
    deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert state['plan_wait']['kind'] == 'round' and deadline == datetime.fromisoformat(original['completed_at']) + timedelta(seconds=30)
    runtime.clock[0] = deadline - timedelta(seconds=1)
    execution.lp_auto_run_once()
    assert len(signed_objects) == 1 and account.posts == [signed] and account.cancels == []
    assert store.lp_actions(action['session_id']) == original_audits
    if phase == 'signing':
        # Current history affects new admission. The real superior LIVE token1
        # still occupies the sole slot, so it is retained without replay.
        advance_api_wait(runtime, execution)
        next_state = execution.lp_auto_run_once(round_id='next-history-round')
        assert next_state['last_round']['round_id'] != original['round_id']
        assert next_state['last_round']['completed_at'] and next_state['active_plan'] is None
        assert [t['token_id'] for t in next_state['last_round']['targets']] == [token]
        assert next_state['last_round']['actions'] == []
        assert account.posts == [signed] and account.cancels == [] and len(signed_objects) == 1
        current, = account.orders
        assert current.id == 'refill-1' and current.token_id == token and current.status == 'LIVE'
        assert Decimal(current.price) == Decimal('.40') and Decimal(current.original_size) == 20
        assert Decimal(current.size_matched) == 0
        assert next_state['slots']['occupied'] == 1 and Decimal(next_state['funds']['buy_reserved_usd']) == 8
        assert store.lp_actions(action['session_id']) == original_audits
        assert datetime.fromisoformat(next_state['plan_wait']['deadline']) == datetime.fromisoformat(
            next_state['last_round']['completed_at']) + timedelta(seconds=30)


def test_unknown_unrelated_account_report_history_does_not_block_new_buys(runtime):
    from tests.test_lp_account_reservation_reconciliation import _position
    _, _, account, _, execution, _ = prepare(runtime, count=2, target=2)
    _, condition, token = _refill_identity(6)
    # Current API inventory cost is known; its absent historical fills cannot
    # reconstruct the report's PnL. Selected tokens keep valid price summaries.
    account.positions = (_position().model_copy(update={'token_id': token, 'condition_id': condition}),)
    planning_as_of, financial_calls = [], []
    sign, balance = account.create_limit_order, account.get_balance_allowance
    def observed_sign(**kwargs):
        planning_as_of.append(execution.lp_auto_state()['funds']['as_of'])
        return sign(**kwargs)
    def observed_balance(**kwargs):
        financial_calls.append(bool(execution.lp_auto_state()['active_plan']))
        return balance(**kwargs)
    account.create_limit_order, account.get_balance_allowance = observed_sign, observed_balance
    state = execution.lp_auto_run_once(round_id='unrelated-report-history')
    assert len(account.posts) == 2, state['last_round']
    assert planning_as_of and planning_as_of[-1]
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == planning_as_of[-1]
    assert state['funds']['spendable_usd'] is None and state['funds']['available_usd'] is None
    assert not any(financial_calls), 'Approved plan must not read balance/allowance'
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = execution.lp_auto_state()
    assert state['funds']['status'] == 'known'
    assert state['funds']['realized_pnl_usd'] is None
    assert Decimal(state['funds']['inventory_cost_usd']) == 8
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
    assert state['slots']['occupied'] == 2


def test_recommendation_yield_and_auto_order_use_the_same_minimum_quantity(runtime):
    import io
    from collections import Counter
    from dataclasses import asdict
    import json

    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24', 3: '12', 4: '6', 5: '3', 6: '1'}
    store, adapter, account, lp, execution, _ = prepare(runtime, public=public)
    # Both public REST boundaries stay offline, through the real adapter.
    def public_response(request, **kwargs):
        if request.data:
            body = json.loads(request.data)
            payload = {'history': {token: [{'t': t, 'p': .4}
                for t in range(body['start_ts'], body['end_ts'] + 1, 60)] for token in body['markets']}}
        else:
            payload = {'data': [{'condition_id': _refill_identity(i)[1], 'market_competitiveness': 10}
                                for i in range(1, 7)], 'next_cursor': 'LTE='}
        return io.BytesIO(json.dumps(payload).encode())
    adapter._urlopen_fn = public_response
    preparation = lp.refresh_price_history()
    assert preparation['state'] == 'known', preparation
    assert lp.refresh_competition_cache()['state'] == 'known'
    lp.refresh_candidates(force=True)
    snapshot = lp.refresh_candidate_recommendations()
    rows = snapshot['candidates']
    assert len(rows) == 6, snapshot
    assert 'estimated_target_quantity' in rows[0], snapshot
    assert Decimal(rows[0]['estimated_target_quantity']) == 20
    assert Decimal(rows[0]['estimated_target_capital_usd']) == 8
    # Independent worked example: bid scores 810 + 640, ask score 810;
    # competition 810 + 640/3, own score 20*.90**2/3.
    assert Decimal(rows[0]['estimated_yield_pct_per_hour']) == Decimal('0.131229')
    planning_as_of, financial_calls = [], []
    sign, balance = account.create_limit_order, account.get_balance_allowance
    def observed_sign(**kwargs):
        planning_as_of.append(execution.lp_auto_state()['funds']['as_of'])
        return sign(**kwargs)
    def observed_balance(**kwargs):
        financial_calls.append(bool(execution.lp_auto_state()['active_plan']))
        return balance(**kwargs)
    account.create_limit_order, account.get_balance_allowance = observed_sign, observed_balance
    state = execution.lp_auto_run_once(round_id='same-yield-basis')
    (store.path.parent/'CI-G-recommendation-native-proof.json').write_text(json.dumps(dict(
        rows=rows,targets=state['last_round']['targets'],candidates=state['last_round']['candidates'],
        posts=[asdict(p) for p in account.posts],state=state,planning_as_of=planning_as_of,
        financial_calls=financial_calls),default=str,indent=2))
    expected = [r['selected_direction']['token_id'] for r in rows[:5]]
    assert Counter(p.token_id for p in account.posts) == Counter(expected)
    assert [r['token_id'] for r in state['last_round']['targets']] == expected
    for token in expected:
        signed, = [p for p in account.posts if p.token_id == token]
        assert signed.maker_amount == 8000000 and signed.taker_amount == 20000000 and signed.post_only is True
    assert state['slots']['active'] == state['slots']['occupied'] == 5
    assert planning_as_of and planning_as_of[-1]
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == planning_as_of[-1]
    assert state['funds']['spendable_usd'] is None and state['funds']['available_usd'] is None
    assert not any(financial_calls), 'Approved plan must not read balance/allowance'
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = execution.lp_auto_state()
    assert state['funds']['status'] == 'known'
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert Decimal(state['last_round']['candidates'][0]['minimum_order_estimate']['yield_pct_per_hour']).quantize(Decimal('.000001')) == Decimal(rows[0]['estimated_yield_pct_per_hour'])


@pytest.mark.parametrize('budget, sizes, expected', [
    ('50', {1: '100', 2: '62.50', 3: '62.50'}, [1]),
    ('30', {1: '100', 2: '62.50', 3: '20'}, [2]),
])
def test_yield_priority_skips_only_unaffordable_candidates(runtime, budget, sizes, expected):
    public = RefillPublic(runtime.clock)
    public.sizes = sizes
    public.rates = {1: '240', 2: '48', 3: '12'}
    _, _, account, _, execution, _ = prepare(runtime, count=3, target=2, budget=budget, public=public)
    planning_as_of, financial_calls = [], []
    sign, balance = account.create_limit_order, account.get_balance_allowance
    def observed_sign(**kwargs):
        planning_as_of.append(execution.lp_auto_state()['funds']['as_of'])
        return sign(**kwargs)
    def observed_balance(**kwargs):
        financial_calls.append(bool(execution.lp_auto_state()['active_plan']))
        return balance(**kwargs)
    account.create_limit_order, account.get_balance_allowance = observed_sign, observed_balance
    state = execution.lp_auto_run_once(round_id='budget-priority')
    assert [p.token_id for p in account.posts] == [_refill_identity(i)[2] for i in expected], state['last_round']
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) <= Decimal(budget)
    assert planning_as_of and planning_as_of[-1]
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == planning_as_of[-1]
    assert state['funds']['spendable_usd'] is None and state['funds']['available_usd'] is None
    assert not any(financial_calls), 'Approved plan must not read balance/allowance'
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = execution.lp_auto_state()
    assert Decimal(state['funds']['buy_reserved_usd']) == (Decimal('40') if budget == '50' else Decimal('25'))
    assert Decimal(state['funds']['spendable_usd']) >= 0


@pytest.mark.parametrize('failure', ['rejected', 'prepare', 'timeout', 'account-unknown', 'pause'])
def test_candidate_failures_and_unknown_account_have_distinct_scope(runtime, failure):
    from dataclasses import asdict
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy
    from datetime import datetime
    from pathlib import Path
    from threading import Event
    from time import monotonic
    import json
    import uuid
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler

    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24', 3: '12'}
    store, adapter, account, lp, execution, _ = prepare(runtime, count=3, target=2, budget='16', public=public)
    tokens = [_refill_identity(i)[2] for i in (1, 2)]
    original_sign, original_post, original_positions = account.create_limit_order, account.post_order, account.list_positions
    phase = ['original_selected_attempts']
    evidence = dict(parameter=failure, reads=[], faults=[], states=[], signer_observations=[])
    first_unknown, release = Event(), Event()
    entered = {t: Event() for t in tokens}; finished = {t: Event() for t in tokens}
    scheduler = LPAutoScheduler(execution, clock=lambda: runtime.clock[0])
    planning_as_of, active_finance = [], []
    balance = account.get_balance_allowance
    def funds(**kwargs):
        active = bool(execution.lp_auto_state()['active_plan'])
        evidence['reads'].append(dict(kind='balance', phase=phase[0], active=active, at=runtime.clock[0]))
        if active:
            active_finance.append(runtime.clock[0])
        return balance(**kwargs)
    account.get_balance_allowance = funds
    for name in ('lp_account_snapshot', 'lp_account_snapshot_shared', 'lp_order_result_snapshot'):
        original = getattr(adapter, name)
        def observed(*args, _method=original, _name=name, **kwargs):
            row = dict(kind=_name, phase=phase[0], at=runtime.clock[0], active=bool(execution.lp_auto_state()['active_plan']))
            evidence['reads'].append(row)
            try:
                value = _method(*args, **kwargs)
            except Exception as exc:
                row['actual_error'] = dict(type=type(exc).__name__, message=str(exc)); raise
            row['packet'] = deepcopy(value)
            return value
        setattr(adapter, name, observed)
    def capture(label):
        state = deepcopy(execution.lp_auto_state())
        evidence['states'].append(dict(label=label, state=state, sessions=store.lp_sessions(),
            audits={s['session_id']: store.lp_actions(s['session_id']) for s in store.lp_sessions()}))
        return state
    def positions_failure(**kwargs):
        row = dict(endpoint='list_positions', phase=phase[0], at=runtime.clock[0])
        evidence['faults'].append(row)
        try:
            raise TimeoutError('offline account read')
        except TimeoutError as exc:
            row['actual_error'] = dict(type=type(exc).__name__, message=str(exc)); raise
    def sign(**kwargs):
        token = kwargs['token_id']; entered[token].set()
        try:
            planning_as_of.append(execution.lp_auto_state()['funds']['as_of'])
            if token == tokens[0] and failure == 'prepare':
                evidence['faults'].append(dict(endpoint='create_limit_order', type='RuntimeError', message='offline prepare failure'))
                raise RuntimeError('offline prepare failure')
            if failure == 'pause':
                assert release.wait(5), 'Real signer/public-pause watchdog'
            if failure == 'timeout' and token == tokens[1]:
                assert first_unknown.wait(5), 'First actual POST must return its unknown outcome'
                deadline = monotonic() + 5
                while True:
                    pending = execution.lp_auto_state()
                    first = next(i for i in pending['intents'] if i['token_id'] == tokens[0])
                    if first['state'] == 'unknown':
                        break
                    assert monotonic() < deadline, 'Public original UNKNOWN outcome watchdog'
                    Event().wait(.01)
                assert not first.get('reservation_coverage')
                assert pending['slots']['occupied'] == 2 and Decimal(pending['funds']['buy_reserved_usd']) == 16
                evidence['signer_observations'].append(deepcopy(pending))
            return original_sign(**kwargs)
        finally:
            finished[token].set()
    def post(signed):
        if signed.token_id == tokens[0]:
            if failure == 'rejected':
                account.posts.append(signed)
                response = {'accepted': False, 'status': 'REJECTED', 'error': 'offline rejection'}
                evidence['faults'].append(dict(endpoint='post_order', response=deepcopy(response)))
                return response
            if failure in ('timeout', 'account-unknown'):
                account.posts.append(signed)
                if failure == 'account-unknown':
                    account.list_positions = positions_failure
                try:
                    raise TimeoutError('offline lost receipt')
                except TimeoutError as exc:
                    evidence['faults'].append(dict(endpoint='post_order', type=type(exc).__name__, message=str(exc)))
                    raise
                finally:
                    first_unknown.set()
        return original_post(signed)
    account.create_limit_order, account.post_order = sign, post
    def quiet(state, elapsed):
        counts = (len(evidence['reads']), len(evidence['faults']), len(account.posts))
        waiting = deepcopy(state['plan_wait']); last = deepcopy(state['last_round'])
        runtime.clock[0] = datetime.fromisoformat(waiting['started_at']) + timedelta(seconds=elapsed)
        assert runtime.clock[0] < datetime.fromisoformat(waiting['deadline'])
        scheduler.request_check(); scheduler.run_due(); execution.lp_auto_run_once(round_id='failure-scope')
        assert counts == (len(evidence['reads']), len(evidence['faults']), len(account.posts))
        assert execution.lp_auto_state()['last_round'] == last and execution.lp_auto_state()['plan_wait'] == waiting
    try:
        if failure == 'pause':
            with ThreadPoolExecutor(max_workers=1) as worker:
                cycle = worker.submit(execution.lp_auto_run_once, round_id='failure-scope')
                try:
                    assert all(e.wait(5) for e in entered.values()), 'Both selected actual signers must enter'
                    assert execution.lp_auto_set_desired_running(False)['pause_confirmed']
                    state = cycle.result(timeout=5)
                    capture('public_local_closure_while_signatures_still_held')
                    assert account.posts == [] and all(not e.is_set() for e in finished.values())
                finally:
                    release.set()
                assert all(e.wait(5) for e in finished.values()), 'Actual returned-signature watchdog'
                deadline = monotonic() + 5
                while True:
                    current = execution.lp_auto_state()
                    if current['slots']['occupied'] == 0:
                        break
                    assert monotonic() < deadline, 'Public physical unsent-cleanup watchdog'
                    Event().wait(.01)
            physical = capture('both_original_unsent_preparations_physically_finished')
            assert physical['pause_confirmed'] and physical['desired_running'] is False
            assert account.posts == [] and physical['slots']['occupied'] == 0
            assert Decimal(physical['funds']['buy_reserved_usd']) == 0
            sessions = store.lp_sessions(); assert len(sessions) == 2
            assert all(s['post_started'] is False and not s.get('entry_order_id') for s in sessions)
            assert all(a['post_started'] is False and not a.get('order_id') for s in sessions for a in store.lp_actions(s['session_id']))
        else:
            state = execution.lp_auto_run_once(round_id='failure-scope')
            capture('original_selected_attempts_locally_complete')
        original_round = deepcopy(state['last_round'])
        assert state['active_plan'] is None and original_round['completed_at']
        assert [r['token_id'] for r in original_round['targets']] == tokens
        assert all(Decimal(r['price']) == Decimal('.40') and Decimal(r['quantity']) == 20 for r in original_round['targets'])
        assert len(original_round['actions']) == 2 and all(a['state'] != 'pending' for a in original_round['actions'])
        assert state['plan_wait']['kind'] == 'round'
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(original_round['completed_at']) + timedelta(seconds=30)
        assert all(p.side == 'BUY' and p.post_only is True and Decimal(p.maker_amount) / p.taker_amount == Decimal('.40')
            and Decimal(p.taker_amount) / 1000000 == 20 for p in account.posts)
        evidence['original_round'] = original_round
        if failure in ('rejected', 'prepare'):
            assert [o.id for o in account.orders] == ['refill-2']
            assert state['slots']['occupied'] == 1 and Decimal(state['funds']['buy_reserved_usd']) == 8
            assert Counter(p.token_id for p in account.posts) == Counter(tokens if failure == 'rejected' else [tokens[1]])
            first = next(a for a in original_round['actions'] if a['token_id'] == tokens[0])
            assert first['state'] == 'rejected'
            native, = store.lp_actions(first['session_id'])
            assert native['state'] == 'rejected' and not native.get('order_id')
            if failure == 'prepare':
                assert first['reason'] == native['reason'] == 'prepare_failed'
                assert native['submit_stage'] == 'prepare_failed' and native['error'] == 'RuntimeError'
                assert native['submit_error_chain'] == ['RuntimeError'] and native['post_started'] is False
            else:
                assert first['reason'] == native['reason'] == 'exchange_rejected'
                assert native['submit_stage'] == 'exchange_rejected' and native['post_started'] is True
        elif failure != 'pause':
            assert Counter(p.token_id for p in account.posts) == Counter(tokens)
            first = next(i for i in state['intents'] if i['token_id'] == tokens[0]); sid = first['session_id']
            original_session = deepcopy(store.lp_session(sid)); audit = deepcopy(store.lp_actions(sid))
            native, = audit; uuid.UUID(native['action_id'])
            assert original_session['submit_status'] == 'unknown' and not original_session.get('entry_order_id')
            assert native['state'] == 'unknown' and native['post_started'] is True and native['error'] == 'TimeoutError'
            assert native['submit_error_chain'] == ['TimeoutError'] and native['submit_timeout'] is True
            assert not first.get('reservation_coverage') and not first.get('order_id')
            assert state['slots']['pending'] == state['slots']['active'] == 1 and state['slots']['occupied'] == 2
            assert Decimal(state['funds']['buy_reserved_usd']) == 16
            assert planning_as_of and planning_as_of[0]
            assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == planning_as_of[0]
            evidence.update(original_session=original_session, original_audit=audit, unknown_sid=sid)
            for elapsed in (1, 29):
                quiet(state, elapsed); assert store.lp_actions(sid) == audit
            if failure == 'account-unknown':
                phase[0] = 'actual_next_current_positions_failure'
                runtime.clock[0] = datetime.fromisoformat(state['plan_wait']['deadline'])
                before = len(evidence['faults'])
                failed = execution.lp_auto_run_once()
                capture('actual_current_required_positions_failure')
                assert len(evidence['faults']) == before + 1 and evidence['faults'][-1]['endpoint'] == 'list_positions'
                assert failed['last_round']['reason'] == 'account_order_sync_unknown'
                assert failed['active_plan'] is None and failed['last_round']['actions'] == []
                assert failed['last_round']['round_id'] != original_round['round_id']
                assert failed['plan_wait']['kind'] == 'round'
                assert datetime.fromisoformat(failed['plan_wait']['deadline']) == datetime.fromisoformat(failed['last_round']['completed_at']) + timedelta(seconds=30)
                assert failed['funds']['status'] == 'unknown' and failed['funds']['as_of'] == planning_as_of[0]
                assert failed['slots']['occupied'] == 2 and Decimal(failed['funds']['buy_reserved_usd']) == 16
                assert len(account.posts) == 2 and store.lp_actions(sid) == audit
                for elapsed in (1, 29):
                    quiet(failed, elapsed); assert store.lp_actions(sid) == audit
                account.list_positions = original_positions
            assert active_finance == []
            phase[0] = 'genuine_complete_public_dashboard'
            assert execution.lp_auto_set_desired_running(False)['pause_confirmed']
            _advance(runtime)
            packet = adapter.lp_account_snapshot_shared(max_age_seconds=-1, trade_generation_provider=store.lp_trade_generation)
            evidence['healthy_full_packet'] = deepcopy(packet)
            assert packet['authenticated'] is True and all(packet[k] is True for k in
                ('open_orders_complete', 'positions_complete', 'trades_complete', 'balance_complete', 'pagination_complete'))
            assert len(packet['open_orders']) == 1 and packet['open_orders'][0]['order_id'] == 'refill-2' and not packet['positions']
            assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
            repaired = capture('complete_public_dashboard_covered_unknown_one_actual_live')
            first = next(i for i in repaired['intents'] if i['session_id'] == sid)
            assert first['reservation_coverage']['state'] == 'covered'
            assert repaired['slots']['occupied'] == repaired['slots']['active'] == 1
            assert Decimal(repaired['funds']['buy_reserved_usd']) == 8 and Decimal(repaired['funds']['spendable_usd']) == 8
            assert repaired['funds']['status'] == 'known' and datetime.fromisoformat(repaired['funds']['as_of']) == packet['checked_at']
            assert repaired['active_plan'] is None and original_round['completed_at']
            assert not first.get('order_id') and first['state'] == 'unknown' and first.get('settled') is not True
            assert Decimal(first['reserved_usd']) == 8 and store.lp_session(sid)['submit_status'] == 'unknown'
            assert not store.lp_session(sid).get('entry_order_id') and store.lp_actions(sid) == audit
            assert len(account.posts) == 2
        if failure in ('rejected', 'prepare', 'pause'):
            locally_ended = execution.lp_auto_state()
            originals = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
            for elapsed in (1, 29):
                quiet(locally_ended, elapsed)
                assert all(store.lp_actions(sid) == rows for sid, rows in originals.items())
        assert active_finance == []
        assert all(p.token_id != _refill_identity(3)[2] for p in account.posts)
        assert account.cancels == account.market_orders == []
        evidence['completed'] = 'full'
    finally:
        release.set()
        evidence.update(phase=phase[0],clock=runtime.clock[0],state=execution.lp_auto_state(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            posts=[asdict(p) for p in account.posts],orders=[o.model_dump(mode='json') for o in account.orders],
            entered={t:e.is_set() for t,e in entered.items()},finished={t:e.is_set() for t,e in finished.items()})
        (Path(store.path).parent/'RC-candidate-diagnostics.json').write_text(json.dumps(evidence,indent=2,default=str)+'\n')


def test_three_to_five_is_one_round_and_concurrent_replay_and_restart_do_not_resend(runtime):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    _, adapter, account, lp, execution, public = prepare(runtime)
    account.orders = tuple(_open_order(f'manual-{i}', 'BUY', price='.40', original='20',
        token_id=_refill_identity(i)[2]).model_copy(update={'market': _refill_identity(i)[1],
                                                         'condition_id': _refill_identity(i)[1]}) for i in range(1, 4))
    entered, release = Event(), Event()
    original_sign = account.create_limit_order
    def delayed_sign(**kwargs):
        entered.set()
        assert release.wait(5), 'Independent real-time watchdog'
        return original_sign(**kwargs)
    account.create_limit_order = delayed_sign
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(execution.lp_auto_run_once, round_id='three-to-five')
        try:
            assert entered.wait(5)
            concurrent = execution.lp_auto_run_once(round_id='concurrent')
            assert concurrent['round_reason'] == 'round_in_progress'
            assert account.posts == []
        finally:
            release.set()
        state = pending.result(timeout=5)
    assert len(account.posts) == 2, state['last_round']
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    replay = execution.lp_auto_run_once(round_id='three-to-five')
    assert replay['slots']['occupied'] == 5
    assert len(account.posts) == 2
    orders = account.orders
    adapter.close()
    _advance(runtime)
    _, _, restarted_account, _, restarted = runtime(public_client=public, orders=orders)
    assert restarted.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    assert restarted.lp_auto_run_once(round_id='three-to-five')['slots']['occupied'] == 5
    assert restarted_account.posts == restarted_account.cancels == []


def test_later_candidate_cannot_jump_a_processed_rank_inside_the_round(runtime):
    from collections import Counter

    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24', 3: '12'}
    _, _, account, _, execution, _ = prepare(runtime, count=3, target=2, public=public)
    original_post = account.post_order
    def post(signed):
        result = original_post(signed)
        if signed.token_id == _refill_identity(1)[2]:
            public.rates[2] = '96'
        return result
    account.post_order = post
    state = execution.lp_auto_run_once(round_id='rank-jump')
    assert Counter(p.token_id for p in account.posts) == Counter(_refill_identity(i)[2] for i in [1, 2]), state['last_round']
    assert _refill_identity(3)[2] not in [p.token_id for p in account.posts]
    assert all(a.get('reason') != 'candidate_rank_changed' for a in state['last_round']['actions'])


@pytest.mark.parametrize('invalid', ['stale', 'identity', 'incomplete', 'generation'])
def test_planned_entry_keeps_real_market_and_identity_fences(runtime, invalid):
    from copy import deepcopy
    from datetime import datetime
    from threading import Barrier, BrokenBarrierError, Event, Lock
    from open_trader.polymarket_trading import TradingConfig
    store, adapter, account, _, execution, public = prepare(runtime, count=2, target=2)
    original_sign = account.create_limit_order
    balance, positions = account.get_balance_allowance, account.list_positions
    forbidden, captured, signed_objects, account_reads, barrier_errors, mutations = [], [], [], [], [], []
    fixture_lock, mutation_complete = Lock(), Event()
    def account_balance(**kwargs):
        active = bool(execution.lp_auto_state()['active_plan'])
        account_reads.append(('balance',runtime.clock[0],active))
        if active:
            forbidden.append(runtime.clock[0])
            raise TimeoutError('approved action must not reread financial transport')
        return balance(**kwargs)
    def account_positions(**kwargs):
        active = bool(execution.lp_auto_state()['active_plan'])
        account_reads.append(('positions',runtime.clock[0],active))
        if active:
            forbidden.append(runtime.clock[0])
        if invalid == 'incomplete' and mutation_complete.is_set():
            return (object(),)
        return positions(**kwargs)
    account.get_balance_allowance, account.list_positions = account_balance, account_positions
    def mutate_after_both_signed():
        assert len(signed_objects) == 2 and len(captured) == 1
        before = runtime.clock[0]
        generation = store.lp_trade_generation()
        advanced = None
        if invalid == 'identity':
            adapter.config = TradingConfig('0x' + 'b' * 40, '0x' + 'b' * 40)
        elif invalid == 'stale':
            _advance(runtime, 61)
        elif invalid == 'generation':
            advanced = store.lp_advance_trade_generation(generation)
            assert store.lp_trade_generation() > generation
        mutations.append(dict(at=before,after=runtime.clock[0],generation=generation,
            resulting_generation=store.lp_trade_generation(),cas_won=advanced))
        mutation_complete.set()
    both_signed = Barrier(2,action=mutate_after_both_signed)
    def sign(**kwargs):
        signed = original_sign(**kwargs)
        with fixture_lock:
            signed_objects.append(signed)
            if not captured:
                captured.append(deepcopy(execution.lp_auto_state()))
        try:
            both_signed.wait(timeout=5)
        except BrokenBarrierError:
            with fixture_lock:
                barrier_errors.append(kwargs['token_id'])
            raise
        assert mutation_complete.is_set(), 'Both genuine signatures precede the one external mutation'
        return signed
    account.create_limit_order = sign
    state = execution.lp_auto_run_once(round_id=f'invalid-{invalid}')
    assert barrier_errors == [] and len(signed_objects) == 2, 'Both real signatures must reach the external seam within the independent 5s watchdog'
    assert len(captured) == 1 and captured[0]['active_plan'] and forbidden == []
    assert len(mutations) == 1 and mutation_complete.is_set()
    assert all(isinstance(s,SignedOrder) and s.maker_amount == 8000000 and s.taker_amount == 20000000
        and s.post_only is True for s in signed_objects)
    assert {s.token_id for s in signed_objects} == {_refill_identity(i)[2] for i in (1,2)}
    if invalid == 'stale':
        assert mutations[0]['after'] == mutations[0]['at'] + timedelta(seconds=61)
    elif invalid == 'generation':
        assert mutations[0]['resulting_generation'] > mutations[0]['generation']
    assert state['last_round']['round_id'] == captured[0]['last_round']['round_id']
    assert state['last_round']['targets'] == captured[0]['last_round']['targets']
    assert [a['action_id'] for a in state['last_round']['actions']] == [a['action_id'] for a in captured[0]['active_plan']['actions']]
    for action in state['last_round']['actions']:
        target = state['last_round']['targets'][action['index']]
        assert target['token_id'] == action['token_id'] and target['condition_id'] == action['condition_id']
        assert Decimal(target['price']) == Decimal('.40') and Decimal(target['quantity']) == 20
    action = state['last_round']['actions'][0]
    if invalid == 'identity':
        assert account.posts == []
        assert state['slots']['occupied'] == 0 and Decimal(state['funds']['buy_reserved_usd']) == 0
        assert all(a['request_state'] == 'entry_rejected' and a['state'] == 'rejected' for a in state['last_round']['actions'])
        assert all(a['reason'] == 'prepare_failed' and a.get('order_id') is None for a in state['last_round']['actions'])
        assert account.orders == ()
    else:
        assert len(account.posts) == 2
        assert {p.token_id for p in account.posts} == {_refill_identity(i)[2] for i in (1, 2)}
        assert all(p.maker_amount == 8000000 and p.taker_amount == 20000000 and p.post_only is True for p in account.posts)
        assert state['slots']['occupied'] == 2 and Decimal(state['funds']['buy_reserved_usd']) == 16
        assert all(a['state'] == 'success' for a in state['last_round']['actions'])
    assert state['last_round']['completed_at'] and state['active_plan'] is None
    assert state['plan_wait']['kind'] == 'round'
    deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert deadline == datetime.fromisoformat(state['last_round']['completed_at']) + timedelta(seconds=30)
    assert forbidden == []
    original_audits = {}
    for action in state['last_round']['actions']:
        assert action.get('session_id')
        audit, = store.lp_actions(action['session_id'])
        assert audit['post_started'] is (invalid != 'identity')
        assert audit['state'] == ('rejected' if invalid == 'identity' else 'accepted')
        assert audit['token_id'] == action['token_id'] and audit['condition_id'] == action['condition_id']
        assert Decimal(audit['price']) == Decimal('.40') and Decimal(audit['quantity']) == 20
        if invalid == 'identity':
            assert audit['reason'] == audit['submit_stage'] == 'prepare_failed'
            assert audit['error'] == 'ValueError' and audit['submit_error_chain'] == ['ValueError']
            assert audit.get('order_id') is None and audit['submit_post_started_at'] is None
            session = store.lp_session(action['session_id'])
            assert session['submit_status'] == 'rejected' and session['submit_stage'] == 'prepare_failed'
            assert session['submit_error_chain'] == ['ValueError']
            assert session.get('entry_order_id') is None and session['submit_post_started_at'] is None
            assert session['token_id'] == action['token_id'] and session['condition_id'] == action['condition_id']
            assert session['account_id'] == WALLET
            assert Decimal(session['price']) == Decimal('.40') and Decimal(session['quantity']) == 20
        original_audits[action['session_id']] = deepcopy(store.lp_actions(action['session_id']))
    before = (len(account_reads),len(signed_objects),len(account.posts),len(account.cancels))
    runtime.clock[0] = deadline - timedelta(seconds=1)
    execution.lp_auto_run_once()
    assert (len(account_reads),len(signed_objects),len(account.posts),len(account.cancels)) == before
    assert {sid:store.lp_actions(sid) for sid in original_audits} == original_audits


def test_definite_allowance_refusal_continues_fixed_plan_then_replans(runtime):
    from collections import Counter
    from types import SimpleNamespace
    public = RefillPublic(runtime.clock)
    public.sizes = {1: '100'}
    public.rates = {1: '240', 2: '24', 3: '12'}
    store, _, account, _, execution, _ = prepare(runtime, count=3, target=2, budget='60', public=public)
    original_sign = account.create_limit_order
    def sign(**kwargs):
        signed = original_sign(**kwargs)
        if kwargs['token_id'] == _refill_identity(1)[2]:
            def balance(**kwargs):
                _advance(runtime)
                return SimpleNamespace(balance='100000000', allowances={WALLET: '30000000'})
            account.get_balance_allowance = balance
        return signed
    account.create_limit_order = sign
    post = account.post_order
    def refusal(signed):
        if signed.token_id == _refill_identity(1)[2]:
            account.posts.append(signed)
            return dict(accepted=False, status='REJECTED', reason='allowance_insufficient')
        return post(signed)
    account.post_order = refusal
    state = execution.lp_auto_run_once(round_id='allowance-drop')
    assert Counter(p.token_id for p in account.posts) == Counter(_refill_identity(i)[2] for i in [1, 2]), state['last_round']
    assert state['last_round']['actions'][0]['state'] == 'rejected'
    assert state['last_round']['actions'][0]['request_state'] == 'entry_rejected'
    action = state['last_round']['actions'][0]
    audit, = store.lp_actions(action['session_id'])
    assert action['reason'] == audit['reason'] == 'allowance_insufficient'
    assert audit['state'] == 'rejected' and audit['post_started'] is True
    first, = [p for p in account.posts if p.token_id == _refill_identity(1)[2]]
    second, = [p for p in account.posts if p.token_id == _refill_identity(2)[2]]
    assert first.maker_amount == 40000000 and first.taker_amount == 100000000
    assert second.maker_amount == 8000000 and second.taker_amount == 20000000
    assert state['last_round']['completed_at'] and state['plan_wait']['kind'] == 'round'
    execution.lp_auto_run_once()
    assert len(account.posts) == 2 and store.lp_actions(action['session_id']) == [audit]
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    advance_api_wait(runtime, execution)
    state = execution.lp_auto_run_once(round_id='next-allowance-round')
    assert Counter(p.token_id for p in account.posts) == Counter(_refill_identity(i)[2] for i in [1, 2, 3])
    assert state['slots']['occupied'] == 2
    assert Decimal(state['funds']['buy_reserved_usd']) == 16




@pytest.mark.parametrize('legacy', [True, False])
def test_restart_never_labels_a_saved_five_percent_estimate_as_minimum_order(runtime, legacy):
    _, adapter, _, lp, _, _ = prepare(runtime, count=1, target=1)
    row = {'condition_id': _refill_identity(1)[1], 'estimate_state': 'known',
           'estimate_basis': 'minimum_scoring_order', 'estimated_yield_raw': '.06561467176462964163048409048',
           'estimated_yield_pct_per_hour': '.065615', 'estimated_target_quantity': '20',
           'estimated_target_capital_usd': '8', 'estimated_hourly_reward_usd': '.005249173741170371330438727238',
           'updated_at': lp._now().isoformat(), 'expires_at': (lp._now() + timedelta(minutes=5)).isoformat()}
    if legacy:
        row.pop('estimate_basis')
        row.update(estimated_yield_raw='.062661', estimated_target_quantity='199.49', estimated_target_capital_usd='79.796')
    # Persist a prior release's public snapshot shape, then use a real restart.
    lp.store.lp_save_screening_snapshot({'pool': {row['condition_id']: row}})
    adapter.close()
    _, _, _, restarted, _ = runtime()
    restored = restarted.candidate_snapshot()['candidates'][0]
    assert restored['updated_at'] == row['updated_at']
    assert restored['expires_at'] == row['expires_at']
    if legacy:
        assert restored['estimate_state'] == 'unknown'
        assert restored['estimated_yield_raw'] is None
        assert restored['estimated_target_capital_usd'] is None
    else:
        assert restored['estimate_state'] == 'known'
        assert Decimal(restored['estimated_target_capital_usd']) == 8


def test_newer_dashboard_publication_preserves_original_plan_authorization(runtime):
    store, _, account, _, execution, public = prepare(runtime, count=2, target=1)
    public.rates = {1: '24', 2: '24'}
    original_sign = account.create_limit_order
    changed, captured, published = [False], [], []
    def sign(**kwargs):
        signed = original_sign(**kwargs)
        if not changed[0]:
            captured.append(execution.lp_auto_state())
            changed[0] = True
            _advance(runtime)
            assert store.lp_advance_trade_generation(store.lp_trade_generation())
            assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
            published.append(execution.lp_auto_state()['funds']['as_of'])
        return signed
    account.create_limit_order = sign
    state = execution.lp_auto_run_once(round_id='presend-generation')
    assert len(account.posts) == 1 and account.posts[0].token_id == _refill_identity(2)[2]
    assert account.posts[0].maker_amount == 8000000 and account.posts[0].taker_amount == 20000000
    assert account.posts[0].post_only is True
    action = state['last_round']['actions'][0]
    assert action['state'] == 'success' and action['request_state'] == 'entry_open'
    assert len(captured) == 1 and captured[0]['active_plan']
    assert state['last_round']['round_id'] == captured[0]['last_round']['round_id']
    assert state['last_round']['targets'] == captured[0]['last_round']['targets']
    assert action['action_id'] == captured[0]['active_plan']['actions'][0]['action_id']
    assert captured[0]['active_plan']['approval_account_facts']['checked_at'] == captured[0]['funds']['as_of']
    assert published[0] != captured[0]['funds']['as_of']
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == published[0]
    assert Decimal(state['last_round']['targets'][0]['price']) == Decimal('.40')
    assert Decimal(state['last_round']['targets'][0]['quantity']) == 20
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('8')
    assert state['slots']['occupied'] == 1
    assert state['active_plan'] is None and state['last_round']['completed_at']
    original_round = state['last_round']['round_id']
    original_action = dict(action)
    original_audit = store.lp_actions(action['session_id'])
    waiting = state['plan_wait']
    from datetime import datetime
    deadline = datetime.fromisoformat(waiting['deadline'])
    assert deadline - datetime.fromisoformat(waiting['started_at']) == timedelta(seconds=30)
    runtime.clock[0] = deadline - timedelta(seconds=1)
    before = (account.position_reads, account.balance_reads, account.order_reads)
    execution.lp_auto_run_once(round_id=original_round)
    assert (account.position_reads, account.balance_reads, account.order_reads) == before
    assert len(account.posts) == 1 and store.lp_actions(action['session_id']) == original_audit
    assert action['token_id'] == _refill_identity(2)[2]
    runtime.clock[0] = deadline
    next_state = execution.lp_auto_run_once(round_id='next-generation-round')
    assert next_state['last_round']['round_id'] != original_round
    assert store.lp_actions(action['session_id']) == original_audit
    assert len(account.posts) == 1
    assert account.posts[0].token_id == _refill_identity(2)[2], state['last_round']
    assert next_state['last_round']['targets'][0]['token_id'] == _refill_identity(2)[2]
    assert not next_state['last_round']['actions']
    assert original_action['state'] == 'success'
    assert original_audit[0]['state'] == 'accepted' and original_audit[0]['post_started'] is True

    assert next_state['active_plan'] is None and next_state['last_round']['completed_at']
    assert next_state['slots']['occupied'] == 1
    assert Decimal(next_state['funds']['buy_reserved_usd']) == Decimal('8')


def test_ui_and_auto_both_reject_the_only_candidate_with_adverse_price_history(runtime):
    store, _, account, lp, execution, _ = prepare(runtime, count=1, target=1)
    _, condition, token = _refill_identity(1)
    lp._candidate_pool_record_success(condition, {'condition_id': condition,
        'selected_direction': {'token_id': token}}, judged_at=lp._now())
    store.lp_save_price_history(condition, token, [], {'state': 'known', 'amplitude': Decimal('.02'),
        'checked_at': lp._now(), 'valid_until': lp._now() + timedelta(days=1)})
    assert lp.candidate_snapshot()['candidate_valid_count'] == 0
    state = execution.lp_auto_run_once(round_id='adverse-only-candidate')
    assert account.posts == []
    assert state['last_round']['actions'] == []
    assert state['last_round']['candidate_filter']['recheck_reasons'] == {'history_amplitude_exceeded': 1}
    assert state['slots']['occupied'] == 0
    assert Decimal(state['funds']['buy_reserved_usd']) == 0


def test_uncovered_unknown_keeps_its_budget_and_slot_across_refill_and_restart(runtime):
    from tests.test_lp_account_reservation_reconciliation import _seed_unknowns
    store, adapter, account, _, execution, public = prepare(runtime, count=3, target=2, budget='16')
    original = _seed_unknowns(store, execution, count=1, stage='sending', explicit_account=True)[0]
    audit = store.lp_actions(original['session_id'])
    planning_as_of = []
    sign = account.create_limit_order
    def capture(**kwargs):
        planning_as_of.append(execution.lp_auto_state()['funds']['as_of'])
        return sign(**kwargs)
    account.create_limit_order = capture
    state = execution.lp_auto_run_once(round_id='uncovered-hold')
    assert [p.token_id for p in account.posts] == [_refill_identity(2)[2]], state['last_round']
    retained = next(i for i in state['intents'] if i['session_id'] == original['session_id'])
    assert not retained.get('reservation_coverage')
    assert state['slots']['occupied'] == 2
    assert state['slots']['pending'] == state['slots']['active'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
    from datetime import datetime
    assert len(planning_as_of) == 1 and planning_as_of[0]
    assert datetime.fromisoformat(planning_as_of[0]) <= runtime.clock[0]
    assert state['funds']['spendable_usd'] is None and state['funds']['status'] == 'unknown'
    assert state['funds']['as_of'] == planning_as_of[0]
    assert store.lp_actions(original['session_id']) == audit
    execution.lp_auto_run_once(round_id='uncovered-hold')
    assert len(account.posts) == 1
    orders = account.orders
    adapter.close()
    _advance(runtime)
    store, _, restarted_account, _, restarted = runtime(public_client=public, orders=orders)
    state = restarted.lp_auto_run_once(round_id='uncovered-restart')
    assert state['slots']['occupied'] == 2
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
    assert not next(i for i in state['intents'] if i['session_id'] == original['session_id']).get('reservation_coverage')
    assert store.lp_actions(original['session_id']) == audit
    assert restarted_account.posts == restarted_account.cancels == []


def _historical_api_buy_intents(runtime):
    """Persist old exact-ID receipt rows whose history cannot be rebuilt."""
    from copy import deepcopy
    store, adapter, account, lp, execution, public = prepare(runtime, budget='40')
    account.orders = tuple(_open_order(f'current-{i}', 'BUY', price='.40', original='20',
        token_id=_refill_identity(i)[2]).model_copy(update={'market': _refill_identity(i)[1],
            'condition_id': _refill_identity(i)[1]}) for i in range(1, 4))
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    originals = []
    for i in (1, 3):
        market, condition, token = _refill_identity(i)
        session = next(s for s in store.lp_sessions() if s['token_id'] == token)
        originals.append(dict(intent_id=f'historical-{i}', session_id=session['session_id'],
            order_id=f'current-{i}', config_version=execution.lp_auto_state()['config_version'],
            state='active', financial_status='unknown', submission_unknown=False,
            reconcile_reason='market_read_capacity', reserved_usd='8', inventory_cost_usd='0',
            realized_pnl_usd='0', price='.40', quantity='20', market_id=market,
            condition_id=condition, token_id=token, outcome='YES',
            created_at=lp._now().isoformat(), checked_at=lp._now().isoformat()))
    execution._auto_pool._update(lambda d: d['intents'].update(
        {i['intent_id']: deepcopy(i) for i in originals}))
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    return store, adapter, account, lp, execution, public, originals


def test_current_api_buys_replace_unknown_history_once_and_refill_after_restart(runtime):
    from collections import Counter
    store, adapter, account, _, execution, public, originals = _historical_api_buy_intents(runtime)
    state = execution.lp_auto_state()
    assert state['slots']['occupied'] == 3, state
    assert Decimal(state['funds']['buy_reserved_usd']) == 24
    assert Decimal(state['funds']['spendable_usd']) == 16
    assert state['funds']['status'] == 'known'
    assert not state['admission_block_reasons']
    assert state['intents'] == originals, 'Projection must retain UNKNOWN receipt audit'
    assert not any(i.get('reservation_coverage') for i in state['intents'])
    audit = {i['session_id']: store.lp_actions(i['session_id']) for i in originals}
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release = Event(), Event()
    original_sign = account.create_limit_order
    def delayed_sign(**kwargs):
        entered.set()
        assert release.wait(5), 'Independent real-time watchdog'
        return original_sign(**kwargs)
    account.create_limit_order = delayed_sign
    planning_as_of, financial_calls = [], []
    sign, balance = account.create_limit_order, account.get_balance_allowance
    def observed_sign(**kwargs):
        planning_as_of.append(execution.lp_auto_state()['funds']['as_of'])
        return sign(**kwargs)
    def observed_balance(**kwargs):
        financial_calls.append(bool(execution.lp_auto_state()['active_plan']))
        return balance(**kwargs)
    account.create_limit_order, account.get_balance_allowance = observed_sign, observed_balance
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(execution.lp_auto_run_once, round_id='unknown-history-three-to-five')
        try:
            assert entered.wait(5)
            concurrent = execution.lp_auto_run_once(round_id='concurrent-history-refill')
            assert concurrent['round_reason'] == 'round_in_progress'
            assert account.posts == []
        finally:
            release.set()
        state = pending.result(timeout=5)
    assert Counter(p.token_id for p in account.posts) == Counter(_refill_identity(i)[2] for i in (4, 5)), state['last_round']
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert planning_as_of and planning_as_of[-1]
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == planning_as_of[-1]
    assert state['funds']['spendable_usd'] is None and state['funds']['available_usd'] is None
    assert not any(financial_calls), 'Approved plan must not read balance/allowance'
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = execution.lp_auto_state()
    assert Decimal(state['funds']['spendable_usd']) == 0
    assert [i for i in state['intents'] if i['intent_id'].startswith('historical-')] == originals
    assert all(store.lp_actions(i['session_id']) == audit[i['session_id']] for i in originals)
    execution.lp_auto_run_once(round_id='unknown-history-three-to-five')
    assert len(account.posts) == 2
    assert account.cancels == []
    orders = account.orders
    adapter.close()
    _advance(runtime)
    store, _, restarted_account, _, restarted = runtime(public_client=public, orders=orders)
    assert restarted.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = restarted.lp_auto_run_once(round_id='unknown-history-three-to-five')
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert [i for i in state['intents'] if i['intent_id'].startswith('historical-')] == originals
    assert all(store.lp_actions(i['session_id']) == audit[i['session_id']] for i in originals)
    assert restarted_account.posts == restarted_account.cancels == []


@pytest.mark.parametrize('invalid', ['stale', 'identity', 'generation', 'financial-unknown',
    'malformed', 'duplicate-conflict', 'missing-token', 'wrong-side'])
def test_invalid_api_facts_do_not_replace_unknown_exact_id_holds(runtime, invalid):
    store, _, account, lp, execution, _, originals = _historical_api_buy_intents(runtime)
    def invalidate(d):
        facts = d['account_financial_facts']
        if invalid == 'stale':
            facts['checked_at'] = (lp._now() - timedelta(seconds=61)).isoformat()
        elif invalid == 'identity':
            facts['account_id'] = '0x' + 'b' * 40
        elif invalid == 'generation':
            facts['trade_generation'] += 1
        elif invalid == 'financial-unknown':
            facts.update(financial_status='unknown', reason_codes=['account_position_cost_unknown'])
        elif invalid == 'malformed':
            facts['buys'] = None
        elif invalid == 'duplicate-conflict':
            facts['buys'].append({**facts['buys'][0], 'reserved_usd': '100'})
        elif invalid == 'missing-token':
            facts['buys'][0]['token_id'] = ''
        else:
            facts['buys'][0]['side'] = 'SELL'
    execution._auto_pool._update(invalidate)
    state = execution.lp_auto_state()
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['spendable_usd'] is None
    assert state['admission_block_reasons']
    assert state['slots']['occupied'] >= 2
    assert Decimal(state['funds']['buy_reserved_usd']) >= 16
    assert state['intents'] == originals
    # A failing real SDK completeness read cannot replace the invalid facts.
    account.list_positions = lambda **kwargs: (object(),)
    state = execution.lp_auto_run_once(round_id=f'invalid-exact-id-{invalid}')
    assert state['admission_block_reasons']
    assert state['intents'] == originals
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('mismatch', ['order', 'token', 'side', 'missing-order', 'missing-token'])
def test_unmatched_unknown_receipt_keeps_full_budget_and_slot(runtime, mismatch):
    _, _, _, _, execution, _, originals = _historical_api_buy_intents(runtime)
    def mismatch_one(d):
        intent = d['intents'][originals[0]['intent_id']]
        if mismatch == 'order':
            intent['order_id'] = 'not-in-api'
        elif mismatch == 'token':
            intent['token_id'] = _refill_identity(2)[2]
        elif mismatch == 'side':
            intent['side'] = 'SELL'
        else:
            intent['order_id' if mismatch == 'missing-order' else 'token_id'] = None
    execution._auto_pool._update(mismatch_one)
    state = execution.lp_auto_state()
    assert state['slots']['occupied'] == 4
    assert Decimal(state['funds']['buy_reserved_usd']) == 32
    if mismatch == 'missing-token':
        assert state['funds']['spendable_usd'] is None
        assert 'unbounded_financial_uncertainty' in state['admission_block_reasons']
    else:
        assert Decimal(state['funds']['spendable_usd']) == 8
    assert state['funds']['status'] == 'unknown'
    assert next(i for i in state['intents'] if i['intent_id'] == originals[0]['intent_id'])['financial_status'] == 'unknown'


@pytest.mark.parametrize('lifecycle', ['unknown', 'sending'])
def test_matching_api_buy_deduplicates_unknown_submission_lifecycle(runtime, lifecycle):
    store, _, account, _, execution, _, originals = _historical_api_buy_intents(runtime)
    execution._auto_pool._update(lambda d: d['intents'][originals[0]['intent_id']].update(
        state=lifecycle, submission_unknown=True))
    audit = execution.lp_auto_state()['intents']
    actions = store.lp_actions(originals[0]['session_id'])
    state = execution.lp_auto_state()
    assert state['slots']['occupied'] == 3
    assert Decimal(state['funds']['buy_reserved_usd']) == 24
    assert Decimal(state['funds']['spendable_usd']) == 16
    assert state['funds']['status'] == 'known'
    assert not state['admission_block_reasons']
    assert state['intents'] == audit
    assert store.lp_actions(originals[0]['session_id']) == actions
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('side, action_state', [('BUY', 'unknown'), ('BUY', 'pending'),
    ('SELL', 'unknown'), ('SELL', 'accepted_without_order_id'), ('SELL', 'accepted')])
def test_matching_api_buy_keeps_independent_unresolved_action_risk(runtime, side, action_state):
    from collections import Counter
    from threading import Barrier
    store, _, account, _, execution, _, originals = _historical_api_buy_intents(runtime)
    original = originals[0]
    store.lp_upsert_action(original['session_id'], f"{original['session_id']}:extra-submit",
        state=action_state, payload={'role': 'augment' if side == 'BUY' else 'passive_exit',
            'side': side, 'token_id': original['token_id'], 'quantity': '20', 'price': '.40',
            'submit_stage': 'sending' if side == 'BUY' else None})
    audit_intents = execution.lp_auto_state()['intents']
    audit_actions = store.lp_actions(original['session_id'])
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = execution.lp_auto_state()
    # Count the represented original BUY once. An independent BUY blocks;
    # SELL retains its audit without reserving BUY capital or quota.
    expected = 4 if side == 'BUY' else 3
    assert state['slots']['occupied'] == expected
    assert Decimal(state['funds']['buy_reserved_usd']) == 8 * expected
    assert state['intents'] == audit_intents
    assert store.lp_actions(original['session_id']) == audit_actions
    if side == 'BUY':
        assert state['funds']['status'] == 'unknown'
        assert state['admission_block_reasons']
        assert state['funds']['spendable_usd'] is None
    else:
        assert state['funds']['status'] == 'known'
        assert Decimal(state['funds']['spendable_usd']) == 16
        assert Decimal(state['funds']['inventory_cost_usd']) == 0
        assert not state['admission_block_reasons']
    assert not next(i for i in state['intents'] if i['intent_id'] == original['intent_id']).get('reservation_coverage')
    planning_as_of = {}
    intended_tokens = {_refill_identity(i)[2] for i in (4, 5)}
    signers = Barrier(2)
    sign = account.create_limit_order
    def capture(**kwargs):
        token = kwargs['token_id']
        planning_as_of.setdefault(token, []).append(execution.lp_auto_state()['funds']['as_of'])
        if side == 'SELL':
            assert token in intended_tokens
            signers.wait(timeout=5)
        return sign(**kwargs)
    account.create_limit_order = capture
    try:
        state = execution.lp_auto_run_once(round_id=f'independent-{side}-{action_state}')
    finally:
        signers.abort()
    if side == 'BUY':
        assert planning_as_of == {}
        assert account.posts == account.cancels == []
    else:
        assert Counter(p.token_id for p in account.posts) == Counter(_refill_identity(i)[2] for i in (4, 5)), state['last_round']
        assert all(p.side == 'BUY' for p in account.posts)
        assert account.cancels == []
        assert state['slots']['occupied'] == 5
        assert Decimal(state['funds']['buy_reserved_usd']) == 40
        from datetime import datetime
        assert set(planning_as_of) == intended_tokens
        assert sum(len(observations) for observations in planning_as_of.values()) == 2
        for observations in planning_as_of.values():
            as_of, = observations
            assert as_of and datetime.fromisoformat(as_of) <= runtime.clock[0]
            assert state['funds']['as_of'] == as_of
        assert state['funds']['spendable_usd'] is None and state['funds']['status'] == 'unknown'
        assert 'account_financial_facts_changed' in state['admission_block_reasons']
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        assert store.lp_actions(original['session_id']) == audit_actions
        _advance(runtime)
        assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        current = execution.lp_auto_state()
        assert current['funds']['status'] == 'known'
        assert Decimal(current['funds']['buy_reserved_usd']) == 40
        assert Decimal(current['funds']['inventory_cost_usd']) == Decimal(current['funds']['spendable_usd']) == 0
        assert current['slots']['occupied'] == 5
        assert len(account.posts) == 2 and account.cancels == []
    original_ids = {i['intent_id'] for i in audit_intents}
    assert [i for i in state['intents'] if i['intent_id'] in original_ids] == audit_intents
    assert store.lp_actions(original['session_id']) == audit_actions


@pytest.mark.parametrize('invalid', ['incomplete', 'failed'])
def test_failed_current_api_read_retains_matching_unknown_holds_until_recovery(runtime, invalid):
    import json
    from copy import deepcopy
    from collections import Counter
    from datetime import datetime
    from dataclasses import asdict,is_dataclass
    store,adapter,account,lp,execution,public,originals=_historical_api_buy_intents(runtime)
    audits={i['session_id']:deepcopy(store.lp_actions(i['session_id'])) for i in originals}
    planning_as_of,financial_calls,packets,position_calls,states=[],[],[],[],[]
    original_sign,read,balance=account.create_limit_order,adapter.lp_account_snapshot_shared,account.get_balance_allowance
    def observed(*args,**kwargs):
        record=dict(at=runtime.clock[0],kwargs={k:getattr(v,'__qualname__',type(v).__name__) if callable(v) else deepcopy(v) for k,v in kwargs.items()})
        try:
            packet=read(*args,**kwargs);record['packet']=deepcopy(packet);return packet
        except BaseException as exc:
            record['error']=dict(type=type(exc).__name__,message=str(exc));raise
        finally:packets.append(record)
    def sign(**kwargs):
        planning_as_of.append(execution.lp_auto_state()['funds']['as_of'])
        return original_sign(**kwargs)
    def observed_balance(**kwargs):
        financial_calls.append(bool(execution.lp_auto_state()['active_plan']));return balance(**kwargs)
    account.create_limit_order=sign
    account.get_balance_allowance=observed_balance
    adapter.lp_account_snapshot_shared=observed
    positions=account.list_positions
    def invalid_positions(**kwargs):
        position_calls.append(dict(at=runtime.clock[0],invalid=invalid))
        if invalid=='failed': raise TimeoutError('offline positions read failed')
        return (object(),)
    account.list_positions=invalid_positions
    try:
        state=execution.lp_auto_run_once(round_id=f'failed-current-api-{invalid}');states.append(deepcopy(state))
        assert state['slots']['occupied']==5 and Decimal(state['funds']['buy_reserved_usd'])==40
        assert state['funds']['spendable_usd'] is None and state['funds']['status']=='unknown'
        assert state['intents']==originals and account.posts==account.cancels==[]
        assert position_calls and {sid:store.lp_actions(sid) for sid in audits}==audits
        account.list_positions=positions
        runtime.clock[0]=datetime.fromisoformat(state['plan_wait']['deadline'])
        recovered=adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
        assert recovered['authenticated'] is True and recovered['wallet_address']==adapter.config.wallet_address
        assert all(recovered[k] is True for k in ('open_orders_complete','positions_complete','trades_complete','pagination_complete'))
        assert recovered['trade_generation']==store.lp_trade_generation()
        assert recovered['read_started_at']<=recovered['read_ended_at']<=recovered['checked_at']
        assert {o['order_id'] for o in recovered['open_orders']}=={'current-1','current-2','current-3'}
        assert tuple(recovered['positions'])==() and tuple(recovered['trades'])==()
        advance_api_wait(runtime,execution,refresh=False)
        state=execution.lp_auto_run_once(round_id=f'recovered-current-api-{invalid}');states.append(deepcopy(state))
        assert Counter(p.token_id for p in account.posts)==Counter(_refill_identity(i)[2] for i in (4,5)),state['last_round']
        assert all(p.maker_amount==8000000 and p.taker_amount==20000000 and p.post_only is True for p in account.posts)
        assert state['slots']['occupied']==5 and Decimal(state['funds']['buy_reserved_usd'])==40
        assert planning_as_of and planning_as_of[0] and datetime.fromisoformat(planning_as_of[0])<=runtime.clock[0]
        assert state['funds']['status']=='unknown' and state['funds']['as_of']==planning_as_of[0]
        assert state['funds']['spendable_usd'] is None and state['funds']['available_usd'] is None
        assert 'account_financial_facts_changed' in state['admission_block_reasons']
        assert [i for i in state['intents'] if i['intent_id'].startswith('historical-')]==originals
        assert {sid:store.lp_actions(sid) for sid in audits}==audits
        assert not any(financial_calls) and account.cancels==account.market_orders==[]
    finally:
        def serialize(value):
            if is_dataclass(value):return asdict(value)
            if hasattr(value,'model_dump'):return value.model_dump(mode='json')
            return str(value)
        (store.path.parent/'CI-G-recovered-read-native-proof.json').write_text(json.dumps(dict(
            invalid=invalid,states=states,packets=packets,position_calls=position_calls,
            planning_as_of=planning_as_of,financial_calls=financial_calls,originals=originals,original_audits=audits,
            current=execution.lp_auto_state(),posts=account.posts,cancels=account.cancels,
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()}),default=serialize,indent=2))


@pytest.mark.parametrize('history', ['rejected-buy', 'terminal-buy', 'unknown-amounts', 'accepted-legacy-sending'])
def test_current_api_buy_replaces_ended_history_and_unknown_report_amounts(runtime, history):
    from collections import Counter
    store, _, account, _, execution, _, originals = _historical_api_buy_intents(runtime)
    original = originals[0]
    if history == 'unknown-amounts':
        execution._auto_pool._update(lambda d: d['intents'][original['intent_id']].update(
            reserved_usd=None, inventory_cost_usd=None))
    elif history == 'accepted-legacy-sending':
        store.lp_update_session(original['session_id'], state='complete', patch={
            'submit_status': 'accepted', 'submit_stage': 'sending'})
        store.lp_upsert_action(original['session_id'], f"{original['session_id']}:entry-submit",
            state='accepted', payload={'role': 'entry', 'side': 'BUY', 'order_id': original['order_id'],
                'token_id': original['token_id']})
    else:
        store.lp_upsert_action(original['session_id'], f"{original['session_id']}:historical-buy",
            state='rejected' if history == 'rejected-buy' else 'complete',
            payload={'role': 'augment', 'side': 'BUY', 'token_id': original['token_id'], 'quantity': '20'})
    audit_intents = execution.lp_auto_state()['intents']
    audit_actions = store.lp_actions(original['session_id'])
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = execution.lp_auto_state()
    assert state['slots']['occupied'] == 3
    assert Decimal(state['funds']['buy_reserved_usd']) == 24
    assert Decimal(state['funds']['inventory_cost_usd']) == 0
    assert Decimal(state['funds']['spendable_usd']) == 16
    assert state['funds']['status'] == 'known'
    assert not state['admission_block_reasons']
    assert state['intents'] == audit_intents
    assert store.lp_actions(original['session_id']) == audit_actions
    if history == 'accepted-legacy-sending':
        assert store.lp_session(original['session_id'])['submit_stage'] == 'sending'
        assert not execution._lp.entry_send_inflight(original['session_id'])
    planning_as_of, financial_calls = [], []
    sign, balance = account.create_limit_order, account.get_balance_allowance
    def observed_sign(**kwargs):
        planning_as_of.append(execution.lp_auto_state()['funds']['as_of'])
        return sign(**kwargs)
    def observed_balance(**kwargs):
        financial_calls.append(bool(execution.lp_auto_state()['active_plan']))
        return balance(**kwargs)
    account.create_limit_order, account.get_balance_allowance = observed_sign, observed_balance
    state = execution.lp_auto_run_once(round_id=f'ended-history-{history}')
    assert Counter(p.token_id for p in account.posts) == Counter(_refill_identity(i)[2] for i in (4, 5)), state['last_round']
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert planning_as_of and planning_as_of[-1]
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == planning_as_of[-1]
    assert state['funds']['spendable_usd'] is None and state['funds']['available_usd'] is None
    assert not any(financial_calls), 'Approved plan must not read balance/allowance'
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = execution.lp_auto_state()
    assert Decimal(state['funds']['spendable_usd']) == 0
    assert [i for i in state['intents'] if i['intent_id'].startswith('historical-')] == audit_intents
    assert store.lp_actions(original['session_id']) == audit_actions


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_known_matching_intent_with_accepted_idless_extra_action_blocks_new_buys(runtime, side):
    from collections import Counter
    store, _, account, _, execution, _, originals = _historical_api_buy_intents(runtime)
    original = originals[0]
    store.lp_upsert_action(original['session_id'], f"{original['session_id']}:accepted-extra-submit",
        state='accepted', payload={'role': 'augment' if side == 'BUY' else 'passive_exit',
            'side': side, 'token_id': original['token_id'], 'quantity': '200',
            'submit_stage': 'sending' if side == 'BUY' else None})
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    execution._auto_pool._update(lambda d: d['intents'][original['intent_id']].update(financial_status='known'))
    audit_intents = execution.lp_auto_state()['intents']
    audit_actions = store.lp_actions(original['session_id'])
    facts, _, reasons = execution._auto_pool._account_projection_facts(execution._auto_pool._read())
    if side == 'BUY':
        assert facts['financial_status'] == 'unknown'
        assert 'account_send_inflight' in reasons
    else:
        assert facts['financial_status'] == 'known' and not reasons
    assert facts['trade_generation'] == store.lp_trade_generation()
    state = execution.lp_auto_state()
    if side == 'BUY':
        assert state['funds']['status'] == 'unknown'
        assert state['funds']['spendable_usd'] is None
        assert 'unbounded_financial_uncertainty' in state['admission_block_reasons']
    else:
        assert state['funds']['status'] == 'known'
        assert Decimal(state['funds']['buy_reserved_usd']) == 24
        assert Decimal(state['funds']['spendable_usd']) == 16
        assert Decimal(state['funds']['inventory_cost_usd']) == 0
        assert not state['admission_block_reasons']
    assert state['slots']['occupied'] == (4 if side == 'BUY' else 3)
    assert state['intents'] == audit_intents
    planning_as_of, financial_calls = [], []
    sign, balance = account.create_limit_order, account.get_balance_allowance
    def observed_sign(**kwargs):
        planning_as_of.append(execution.lp_auto_state()['funds']['as_of'])
        return sign(**kwargs)
    def observed_balance(**kwargs):
        financial_calls.append(bool(execution.lp_auto_state()['active_plan']))
        return balance(**kwargs)
    account.create_limit_order, account.get_balance_allowance = observed_sign, observed_balance
    state = execution.lp_auto_run_once(round_id=f'known-idless-extra-{side}')
    if side == 'BUY':
        assert 'unbounded_financial_uncertainty' in state['admission_block_reasons']
        assert state['funds']['spendable_usd'] is None
        assert account.posts == account.cancels == []
    else:
        assert Counter(p.token_id for p in account.posts) == Counter(_refill_identity(i)[2] for i in (4, 5)), state['last_round']
        assert all(p.side == 'BUY' for p in account.posts)
        assert account.cancels == []
        assert state['slots']['occupied'] == 5
        assert Decimal(state['funds']['buy_reserved_usd']) == 40
        assert planning_as_of and planning_as_of[-1]
        assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == planning_as_of[-1]
        assert state['funds']['spendable_usd'] is None and state['funds']['available_usd'] is None
        assert not any(financial_calls), 'Approved plan must not read balance/allowance'
        _advance(runtime)
        assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        state = execution.lp_auto_state()
        assert Decimal(state['funds']['spendable_usd']) == 0
        assert state['funds']['status'] == 'known'
        assert not state['admission_block_reasons']
    original_ids = {i['intent_id'] for i in audit_intents}
    assert [i for i in state['intents'] if i['intent_id'] in original_ids] == audit_intents
    assert store.lp_actions(original['session_id']) == audit_actions


def test_current_read_failures_preserve_original_unknown_cause_and_audit(runtime):
    from dataclasses import asdict
    from collections import Counter
    from copy import deepcopy
    from datetime import datetime
    from pathlib import Path
    import json
    import uuid
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler

    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24'}
    store, adapter, account, lp, execution, _ = prepare(runtime, count=2, target=2, public=public)
    tokens = [_refill_identity(i)[2] for i in (1, 2)]
    balance, post, sign = account.get_balance_allowance, account.post_order, account.create_limit_order
    phase = ['original_selected_attempts']
    evidence = dict(reads=[],faults=[],states=[])
    financial, forbidden = [], []
    def balances(**kwargs):
        active = bool(execution.lp_auto_state()['active_plan'])
        evidence['reads'].append(dict(kind='balance',phase=phase[0],active=active,at=runtime.clock[0]))
        if active:
            forbidden.append(runtime.clock[0])
            raise TimeoutError('financial transport cannot approve execution')
        return balance(**kwargs)
    account.get_balance_allowance = balances
    for name in ('lp_account_snapshot', 'lp_account_snapshot_shared', 'lp_order_result_snapshot'):
        original = getattr(adapter, name)
        def observed(*args, _method=original, _name=name, **kwargs):
            row = dict(kind=_name, phase=phase[0], at=runtime.clock[0],active=bool(execution.lp_auto_state()['active_plan']))
            evidence['reads'].append(row)
            try:
                value = _method(*args, **kwargs)
            except Exception as exc:
                row['actual_error']=dict(type=type(exc).__name__,message=str(exc));raise
            row['packet']=deepcopy(value);row['ended_at']=runtime.clock[0]
            return value
        setattr(adapter,name,observed)
    def capture(label):
        state=deepcopy(execution.lp_auto_state())
        evidence['states'].append(dict(label=label,state=state,sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()}))
        return state
    def signing(**kwargs):
        financial.append(execution.lp_auto_state()['funds']['as_of'])
        return sign(**kwargs)
    account.create_limit_order=signing
    def unknown(signed):
        if signed.token_id==tokens[0]:
            account.posts.append(signed)
            try:
                raise TimeoutError('original actual POST outcome unknown')
            except TimeoutError as exc:
                evidence['original_post_error']=dict(type=type(exc).__name__,message=str(exc),at=runtime.clock[0]);raise
        return post(signed)
    account.post_order=unknown
    scheduler=LPAutoScheduler(execution,clock=lambda:runtime.clock[0])
    failed_at=[]
    try:
        state=execution.lp_auto_run_once(round_id='latched-result-failure')
        capture('original_selected_attempts_locally_complete')
        original_round=deepcopy(state['last_round'])
        assert state['active_plan'] is None and original_round['completed_at']
        assert state['plan_wait']['kind']=='round'
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(original_round['completed_at'])+timedelta(seconds=30)
        assert [r['token_id'] for r in original_round['targets']]==tokens
        assert all(Decimal(r['price'])==Decimal('.40') and Decimal(r['quantity'])==20 for r in original_round['targets'])
        assert Counter(p.token_id for p in account.posts)==Counter(tokens)
        assert all(p.side=='BUY' and p.post_only is True and Decimal(p.maker_amount)/p.taker_amount==Decimal('.40')
            and Decimal(p.taker_amount)/1000000==20 for p in account.posts)
        first=next(a for a in original_round['actions'] if a['token_id']==tokens[0])
        sibling=next(a for a in original_round['actions'] if a['token_id']==tokens[1])
        assert first['state']=='unknown' and sibling['state']=='success'
        sid=first['session_id'];session=deepcopy(store.lp_session(sid));audit=deepcopy(store.lp_actions(sid))
        row,=audit;uuid.UUID(row['action_id'])
        assert row['state']=='unknown' and row['post_started'] is True and row['error']=='TimeoutError'
        assert row['submit_error_chain']==['TimeoutError'] and row['submit_timeout'] is True
        assert session['submit_status']=='unknown' and not session.get('entry_order_id')
        assert state['slots']['occupied']==2 and Decimal(state['funds']['buy_reserved_usd'])==16
        assert financial[0] and datetime.fromisoformat(financial[0])<=runtime.clock[0]
        assert state['funds']['status']=='unknown' and state['funds']['as_of']==financial[0]
        originals={s['session_id']:deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
        evidence.update(original_round=original_round,original_action=first,original_session=session,original_audits=originals,unknown_sid=sid)
        open_orders=account.list_open_orders
        def failure(**kwargs):
            _advance(runtime,3);failed_at.append(runtime.clock[0])
            observed=dict(endpoint='list_open_orders',at=runtime.clock[0],phase=phase[0],kwargs=deepcopy(kwargs))
            evidence['faults'].append(observed)
            if len(failed_at)==1:
                try:
                    raise TimeoutError('first actual result read failure')
                except TimeoutError as exc:
                    observed['actual_error']=dict(type=type(exc).__name__,message=str(exc));raise
            actual=open_orders(**kwargs)
            returned=tuple(o.model_copy(update={'token_id':'wrong-owned-token'}) for o in actual)
            observed.update(original_sdk=[o.model_dump(mode='json') for o in actual],returned_sdk=[o.model_dump(mode='json') for o in returned])
            assert len(returned)==1 and returned[0].id==actual[0].id==sibling['order_id']
            assert returned[0].token_id=='wrong-owned-token' and actual[0].token_id==tokens[1]
            return returned
        account.list_open_orders=failure
        for cause in ('account_order_sync_unknown','order_identity_conflict'):
            phase[0]='actual_required_current_'+cause
            prior=state['last_round']['round_id'];before=len(failed_at)
            runtime.clock[0]=datetime.fromisoformat(state['plan_wait']['deadline'])
            state=execution.lp_auto_run_once()
            capture(cause)
            assert len(failed_at)==before+1
            assert state['last_round']['reason']==cause
            assert state['active_plan'] is None and state['last_round']['actions']==[]
            assert state['last_round']['round_id'] not in (prior,original_round['round_id'])
            assert state['last_round']['completed_at']
            assert store.lp_actions(sid)==audit and len(account.posts)==2
            assert store.lp_session(sid)['submit_status']=='unknown' and not store.lp_session(sid).get('entry_order_id')
            assert all(store.lp_actions(original_sid)==rows for original_sid,rows in originals.items())
            assert state['funds']['status']=='unknown' and state['funds']['as_of']==financial[0]
            assert state['slots']['occupied']==2 and Decimal(state['funds']['buy_reserved_usd'])==16
            waiting=deepcopy(state['plan_wait']);last=deepcopy(state['last_round'])
            assert waiting['kind']=='round'
            assert datetime.fromisoformat(waiting['started_at'])==datetime.fromisoformat(last['completed_at'])
            assert datetime.fromisoformat(waiting['started_at'])>=failed_at[-1]
            assert datetime.fromisoformat(waiting['deadline'])==datetime.fromisoformat(waiting['started_at'])+timedelta(seconds=30)
            for elapsed in (1,29):
                counts=(len(evidence['reads']),len(failed_at),len(account.posts))
                runtime.clock[0]=datetime.fromisoformat(waiting['started_at'])+timedelta(seconds=elapsed)
                assert runtime.clock[0]<datetime.fromisoformat(waiting['deadline'])
                scheduler.request_check();scheduler.run_due();execution.lp_auto_run_once()
                assert counts==(len(evidence['reads']),len(failed_at),len(account.posts))
                assert execution.lp_auto_state()['last_round']==last and execution.lp_auto_state()['plan_wait']==waiting
                assert store.lp_actions(sid)==audit
        assert len(failed_at)==2 and forbidden==[]
        account.list_open_orders=open_orders
        phase[0]='paused_healthy_current_check'
        assert execution.lp_auto_set_desired_running(False)['pause_confirmed']
        runtime.clock[0]=datetime.fromisoformat(state['plan_wait']['deadline'])
        checked=execution.lp_auto_run_once()
        capture('actual_paused_healthy_private_observation_before_dashboard')
        assert checked['active_plan'] is None and checked['last_round']['actions']==[] and checked['last_round']['checked_at']
        assert checked['desired_running'] is False and len(account.posts)==2
        assert store.lp_actions(sid)==audit
        phase[0]='genuine_complete_public_dashboard'
        packet=adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
        evidence['healthy_full_packet']=deepcopy(packet)
        assert packet['authenticated'] is True and all(packet[k] is True for k in
            ('open_orders_complete','positions_complete','trades_complete','balance_complete','pagination_complete'))
        assert len(packet['open_orders'])==1 and packet['open_orders'][0]['order_id']==sibling['order_id'] and not packet['positions']
        assert execution.refresh_lp_dashboard_snapshot()['state']=='ready'
        recovered=capture('actual_public_healthy_publication_clears_sync_error')
        assert recovered['active_plan'] is None and recovered['desired_running'] is False
        assert recovered['funds']['status']=='known' and datetime.fromisoformat(recovered['funds']['as_of'])==packet['checked_at']
        assert Decimal(recovered['funds']['buy_reserved_usd'])==8 and recovered['slots']['occupied']==recovered['slots']['active']==1
        assert not any(r in ('account_order_sync_unknown','order_identity_conflict') for r in recovered['block_reasons'])
        first_intent=next(i for i in recovered['intents'] if i['session_id']==sid)
        assert first_intent['reservation_coverage']['state']=='covered'
        assert first_intent['state']=='unknown' and first_intent.get('settled') is not True and not first_intent.get('order_id')
        assert Decimal(first_intent['reserved_usd'])==8 and store.lp_session(sid)['submit_status']=='unknown'
        assert not store.lp_session(sid).get('entry_order_id') and store.lp_actions(sid)==audit
        assert all(store.lp_actions(original_sid)==rows for original_sid,rows in originals.items())
        assert len(account.posts)==2 and account.cancels==account.market_orders==[]
        assert forbidden==[] and len(failed_at)==2
        evidence['completed']='full'
    finally:
        evidence.update(phase=phase[0],clock=runtime.clock[0],state=execution.lp_auto_state(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            posts=[asdict(p) for p in account.posts],orders=[o.model_dump(mode='json') for o in account.orders],failed_at=failed_at)
        (Path(store.path).parent/'RC-causal-diagnostics.json').write_text(json.dumps(evidence,indent=2,default=str)+'\n')
