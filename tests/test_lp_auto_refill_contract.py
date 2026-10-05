"""Issue #249: public auto-buy contracts with the real adapter and SQLite."""
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
    state = execution.lp_auto_run_once(round_id='stale-candidate-account')
    assert len(account.posts) == 2
    assert state['slots']['occupied'] == 2
    assert state['funds']['status'] == 'known'


@pytest.mark.parametrize('change', ['rank', 'price-same-rank'])
def test_presend_relative_rank_change_skips_only_this_candidate(runtime, change):
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
    assert tokens == [_refill_identity(i)[2] for i in ([2, 3] if change == 'rank' else [1, 2])], state['last_round']
    if change == 'rank':
        assert state['last_round']['actions'][0]['reason'] == 'candidate_rank_changed'
    else:
        assert account.posts[0].maker_amount == 7800000
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
    if phase == 'preparation':
        invalidate()
        assert lp.candidate_snapshot()['candidate_valid_count'] == 1
    else:
        original_sign = account.create_limit_order
        def sign(**kwargs):
            signed = original_sign(**kwargs)
            if kwargs['token_id'] == token:
                invalidate()
            return signed
        account.create_limit_order = sign
    state = execution.lp_auto_run_once(round_id=f'price-history-{phase}-{invalid}')
    assert [p.token_id for p in account.posts] == [_refill_identity(2)[2]], state['last_round']
    assert state['last_round']['actions'][0]['reason'] == reason
    assert state['last_round']['actions'][0]['state'] == ('rejected' if phase == 'preparation' else 'entry_rejected')
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    assert not state['admission_block_reasons']


def test_unknown_unrelated_account_report_history_does_not_block_new_buys(runtime):
    from tests.test_lp_account_reservation_reconciliation import _position
    _, _, account, _, execution, _ = prepare(runtime, count=2, target=2)
    _, condition, token = _refill_identity(6)
    # Current API inventory cost is known; its absent historical fills cannot
    # reconstruct the report's PnL. Selected tokens keep valid price summaries.
    account.positions = (_position().model_copy(update={'token_id': token, 'condition_id': condition}),)
    state = execution.lp_auto_run_once(round_id='unrelated-report-history')
    assert len(account.posts) == 2, state['last_round']
    assert state['funds']['status'] == 'known'
    assert state['funds']['realized_pnl_usd'] is None
    assert Decimal(state['funds']['inventory_cost_usd']) == 8
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
    assert state['slots']['occupied'] == 2


def test_recommendation_yield_and_auto_order_use_the_same_minimum_quantity(runtime):
    import io
    import json

    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24', 3: '12', 4: '6', 5: '3', 6: '1'}
    _, adapter, account, lp, execution, _ = prepare(runtime, public=public)
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
    state = execution.lp_auto_run_once(round_id='same-yield-basis')
    assert [p.token_id for p in account.posts] == [r['selected_direction']['token_id'] for r in rows[:5]]
    assert state['slots']['active'] == state['slots']['occupied'] == 5
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
    state = execution.lp_auto_run_once(round_id='budget-priority')
    assert [p.token_id for p in account.posts] == [_refill_identity(i)[2] for i in expected], state['last_round']
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) <= Decimal(budget)
    assert Decimal(state['funds']['spendable_usd']) >= 0


@pytest.mark.parametrize('failure', ['rejected', 'prepare', 'timeout', 'account-unknown', 'pause'])
def test_candidate_failures_and_unknown_account_have_distinct_scope(runtime, failure):
    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24', 3: '12'}
    store, _, account, _, execution, _ = prepare(runtime, count=3, target=2, budget='16', public=public)
    original_sign, original_post = account.create_limit_order, account.post_order

    def sign(**kwargs):
        if kwargs['token_id'] == _refill_identity(1)[2]:
            if failure == 'prepare':
                raise RuntimeError('offline prepare failure')
            if failure == 'pause':
                execution.lp_auto_set_desired_running(False)
        if failure == 'timeout' and kwargs['token_id'] == _refill_identity(2)[2]:
            pending = execution.lp_auto_state()
            first = next(i for i in pending['intents'] if i['token_id'] == _refill_identity(1)[2])
            assert first['state'] == 'unknown' and not first.get('reservation_coverage')
            assert pending['slots']['occupied'] == 2
            assert Decimal(pending['funds']['buy_reserved_usd']) == 16
        return original_sign(**kwargs)

    def post(signed):
        if signed.token_id == _refill_identity(1)[2]:
            if failure == 'rejected':
                account.posts.append(signed)
                return {'accepted': False, 'status': 'REJECTED', 'error': 'offline rejection'}
            if failure in ('timeout', 'account-unknown'):
                account.posts.append(signed)
                if failure == 'account-unknown':
                    account.list_positions = lambda **kwargs: (_ for _ in ()).throw(TimeoutError('offline account read'))
                raise TimeoutError('offline lost receipt')
        return original_post(signed)

    account.create_limit_order, account.post_order = sign, post
    state = execution.lp_auto_run_once(round_id='failure-scope')
    if failure in ('rejected', 'prepare'):
        assert [o.id for o in account.orders] == ['refill-2', 'refill-3'], state['last_round']
        assert state['slots']['occupied'] == 2
        assert Decimal(state['funds']['buy_reserved_usd']) == 16
    elif failure == 'pause':
        assert account.posts == []
        assert state['pause_confirmed']
        assert state['slots']['occupied'] == 0
    elif failure == 'account-unknown':
        assert len(account.posts) == 1
        assert state['admission_block_reasons']
        assert state['last_round']['reason'] in state['admission_block_reasons']
        assert state['slots']['occupied'] == 1
        assert Decimal(state['funds']['buy_reserved_usd']) == 8
    else:
        assert [p.token_id for p in account.posts] == [_refill_identity(i)[2] for i in (1, 2, 3)], state['last_round']
        first = next(i for i in state['intents'] if i['token_id'] == _refill_identity(1)[2])
        sid = first['session_id']
        assert store.lp_session(sid)['submit_status'] == 'unknown'
        assert store.lp_actions(sid)[0]['state'] == 'unknown'
        assert first['reservation_coverage']['state'] == 'covered'
        assert state['funds']['status'] == 'known'
        assert not state['admission_block_reasons']
        assert state['slots']['active'] == state['slots']['occupied'] == 2
        assert Decimal(state['funds']['buy_reserved_usd']) == 16
        assert Decimal(state['funds']['spendable_usd']) == 0
        audit = store.lp_actions(sid)
        execution.lp_auto_run_once(round_id='failure-scope')
        assert len(account.posts) == 3
        _advance(runtime)
        execution.refresh_lp_dashboard_snapshot()
        repaired = execution.lp_auto_state()
        first = next(i for i in repaired['intents'] if i['session_id'] == sid)
        assert first['reservation_coverage']['state'] == 'covered'
        assert repaired['slots']['occupied'] == 2
        assert Decimal(repaired['funds']['buy_reserved_usd']) == 16
        assert store.lp_actions(sid) == audit
        assert store.lp_session(sid)['submit_status'] == 'unknown'
        assert store.lp_actions(sid)[0]['state'] == 'unknown'
    assert account.cancels == account.market_orders == []


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
    assert [p.token_id for p in account.posts] == [_refill_identity(i)[2] for i in [1, 3]], state['last_round']
    assert state['last_round']['actions'][1]['reason'] == 'candidate_rank_changed'


@pytest.mark.parametrize('invalid', ['stale', 'identity', 'incomplete', 'generation'])
def test_presend_account_fences_still_block_real_adapter_submissions(runtime, invalid):
    from open_trader.polymarket_trading import TradingConfig
    store, adapter, account, _, execution, public = prepare(runtime, count=2, target=2)
    original_sign = account.create_limit_order
    def sign(**kwargs):
        signed = original_sign(**kwargs)
        if invalid == 'identity':
            adapter.config = TradingConfig('0x' + 'b' * 40, '0x' + 'b' * 40)
        elif invalid == 'incomplete':
            account.list_positions = lambda **kwargs: (object(),)
        elif invalid == 'stale':
            # Account facts read before the slow market response must expire.
            original_book = public.get_order_book
            def delayed_book(**kwargs):
                _advance(runtime, 61)
                return original_book(**kwargs)
            public.get_order_book = delayed_book
        else:
            account.before_positions = lambda n: store.lp_advance_trade_generation(store.lp_trade_generation())
        return signed
    account.create_limit_order = sign
    state = execution.lp_auto_run_once(round_id=f'invalid-{invalid}')
    assert account.posts == [], state['last_round']
    assert state['slots']['occupied'] == 0
    assert Decimal(state['funds']['buy_reserved_usd']) == 0
    assert state['last_round']['actions'][0]['state'] == 'entry_rejected'


def test_sendtime_allowance_drop_skips_expensive_candidate_then_uses_api_remainder(runtime):
    from types import SimpleNamespace
    public = RefillPublic(runtime.clock)
    public.sizes = {1: '100'}
    public.rates = {1: '240', 2: '24', 3: '12'}
    _, _, account, _, execution, _ = prepare(runtime, count=3, target=2, budget='60', public=public)
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
    state = execution.lp_auto_run_once(round_id='allowance-drop')
    assert [p.token_id for p in account.posts] == [_refill_identity(i)[2] for i in [2, 3]], state['last_round']
    assert state['last_round']['actions'][0]['state'] == 'entry_rejected'
    assert state['slots']['occupied'] == 2
    assert Decimal(state['funds']['buy_reserved_usd']) == 16


def test_a_presend_account_read_failure_ends_refill_even_if_next_read_recovers(runtime):
    _, _, account, _, execution, _ = prepare(runtime, count=2, target=2)
    original_sign, positions = account.create_limit_order, account.list_positions
    failed = [False]
    def read_positions(**kwargs):
        if not failed[0]:
            failed[0] = True
            raise TimeoutError('offline single account failure')
        return positions(**kwargs)
    def sign(**kwargs):
        signed = original_sign(**kwargs)
        account.list_positions = read_positions
        return signed
    account.create_limit_order = sign
    state = execution.lp_auto_run_once(round_id='one-account-failure')
    assert account.posts == [], state['last_round']
    assert state['last_round']['reason'] == 'account_order_sync_unknown'
    assert state['slots']['occupied'] == 0


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


def test_newer_dashboard_publication_cannot_make_an_old_presend_snapshot_current(runtime):
    store, _, account, _, execution, public = prepare(runtime, count=2, target=1)
    public.rates = {1: '24', 2: '24'}
    original_sign, original_book = account.create_limit_order, public.get_order_book
    changed = [False]
    def book(**kwargs):
        if not changed[0]:
            changed[0] = True
            _advance(runtime)
            assert store.lp_advance_trade_generation(store.lp_trade_generation())
            assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        return original_book(**kwargs)
    def sign(**kwargs):
        signed = original_sign(**kwargs)
        public.get_order_book = book
        return signed
    account.create_limit_order = sign
    state = execution.lp_auto_run_once(round_id='presend-generation')
    assert len(account.posts) == 1
    assert account.posts[0].token_id == _refill_identity(1)[2], state['last_round']
    assert state['last_round']['actions'][0]['state'] == 'entry_rejected'
    assert state['last_round']['actions'][0]['reason'] == 'account_financial_facts_changed'


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
    assert state['last_round']['actions'][0]['reason'] == 'history_amplitude_exceeded'
    assert state['slots']['occupied'] == 0
    assert Decimal(state['funds']['buy_reserved_usd']) == 0


def test_uncovered_unknown_keeps_its_budget_and_slot_across_refill_and_restart(runtime):
    from tests.test_lp_account_reservation_reconciliation import _seed_unknowns
    store, adapter, account, _, execution, public = prepare(runtime, count=3, target=2, budget='16')
    original = _seed_unknowns(store, execution, count=1, stage='sending', explicit_account=True)[0]
    audit = store.lp_actions(original['session_id'])
    state = execution.lp_auto_run_once(round_id='uncovered-hold')
    assert [p.token_id for p in account.posts] == [_refill_identity(2)[2]], state['last_round']
    retained = next(i for i in state['intents'] if i['session_id'] == original['session_id'])
    assert not retained.get('reservation_coverage')
    assert state['slots']['occupied'] == 2
    assert state['slots']['pending'] == state['slots']['active'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
    assert Decimal(state['funds']['spendable_usd']) == 0
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
    assert [p.token_id for p in account.posts] == [_refill_identity(i)[2] for i in (4, 5)], state['last_round']
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
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


@pytest.mark.parametrize('mismatch', ['order', 'token', 'side', 'missing-order', 'missing-token', 'submission-unknown', 'sending'])
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
        elif mismatch in ('submission-unknown', 'sending'):
            intent.update(state='unknown' if mismatch == 'submission-unknown' else 'sending', submission_unknown=True)
        else:
            intent['order_id' if mismatch == 'missing-order' else 'token_id'] = None
    execution._auto_pool._update(mismatch_one)
    state = execution.lp_auto_state()
    assert state['slots']['occupied'] == 4
    assert Decimal(state['funds']['buy_reserved_usd']) == 32
    assert Decimal(state['funds']['spendable_usd']) == 8
    assert state['funds']['status'] == 'unknown'
    assert next(i for i in state['intents'] if i['intent_id'] == originals[0]['intent_id'])['financial_status'] == 'unknown'


@pytest.mark.parametrize('side, action_state', [('BUY', 'unknown'), ('BUY', 'pending'),
    ('SELL', 'unknown'), ('SELL', 'accepted_without_order_id'), ('SELL', 'accepted')])
def test_matching_api_buy_keeps_independent_unresolved_action_risk(runtime, side, action_state):
    store, _, account, _, execution, _, originals = _historical_api_buy_intents(runtime)
    original = originals[0]
    store.lp_upsert_action(original['session_id'], f"{original['session_id']}:extra-submit",
        state=action_state, payload={'role': 'augment' if side == 'BUY' else 'passive_exit',
            'side': side, 'token_id': original['token_id'], 'quantity': '20'})
    audit_intents = execution.lp_auto_state()['intents']
    audit_actions = store.lp_actions(original['session_id'])
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = execution.lp_auto_state()
    # A pending extra BUY invalidates account admission itself, retaining both
    # historical holds. Ended/SELL uncertainty still retains its entire hold.
    expected = 5 if side == 'BUY' and action_state == 'pending' else 4
    assert state['slots']['occupied'] == expected
    assert Decimal(state['funds']['buy_reserved_usd']) == 8 * expected
    assert state['funds']['status'] == 'unknown'
    assert state['intents'] == audit_intents
    assert store.lp_actions(original['session_id']) == audit_actions
    assert state['admission_block_reasons']
    assert state['funds']['spendable_usd'] is None
    execution.lp_auto_run_once(round_id=f'independent-{side}-{action_state}')
    assert account.posts == account.cancels == []
    assert not next(i for i in state['intents'] if i['intent_id'] == original['intent_id']).get('reservation_coverage')


@pytest.mark.parametrize('invalid', ['incomplete', 'failed'])
def test_failed_current_api_read_retains_matching_unknown_holds_until_recovery(runtime, invalid):
    _, _, account, _, execution, _, originals = _historical_api_buy_intents(runtime)
    positions = account.list_positions
    def invalid_positions(**kwargs):
        if invalid == 'failed':
            raise TimeoutError('offline positions read failed')
        return (object(),)
    account.list_positions = invalid_positions
    state = execution.lp_auto_run_once(round_id=f'failed-current-api-{invalid}')
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert state['funds']['spendable_usd'] is None
    assert state['funds']['status'] == 'unknown'
    assert state['intents'] == originals
    assert account.posts == account.cancels == []
    account.list_positions = positions
    _advance(runtime)
    state = execution.lp_auto_run_once(round_id=f'recovered-current-api-{invalid}')
    assert len(account.posts) == 2, state['last_round']
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert state['funds']['status'] == 'known'
    assert not state['admission_block_reasons']


@pytest.mark.parametrize('history', ['rejected-buy', 'terminal-buy', 'unknown-amounts', 'accepted-legacy-sending'])
def test_current_api_buy_replaces_ended_history_and_unknown_report_amounts(runtime, history):
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
    state = execution.lp_auto_run_once(round_id=f'ended-history-{history}')
    assert [p.token_id for p in account.posts] == [_refill_identity(i)[2] for i in (4, 5)], state['last_round']
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert Decimal(state['funds']['spendable_usd']) == 0
    assert [i for i in state['intents'] if i['intent_id'].startswith('historical-')] == audit_intents
    assert store.lp_actions(original['session_id']) == audit_actions


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_known_matching_intent_with_accepted_idless_extra_action_blocks_new_buys(runtime, side):
    store, _, account, _, execution, _, originals = _historical_api_buy_intents(runtime)
    original = originals[0]
    store.lp_upsert_action(original['session_id'], f"{original['session_id']}:accepted-extra-submit",
        state='accepted', payload={'role': 'augment' if side == 'BUY' else 'passive_exit',
            'side': side, 'token_id': original['token_id'], 'quantity': '200'})
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    execution._auto_pool._update(lambda d: d['intents'][original['intent_id']].update(financial_status='known'))
    audit_intents = execution.lp_auto_state()['intents']
    audit_actions = store.lp_actions(original['session_id'])
    facts, _, reasons = execution._auto_pool._account_projection_facts(execution._auto_pool._read())
    assert facts['financial_status'] == 'known' and not reasons
    assert facts['trade_generation'] == store.lp_trade_generation()
    state = execution.lp_auto_state()
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['spendable_usd'] is None
    assert 'unbounded_financial_uncertainty' in state['admission_block_reasons']
    assert state['slots']['occupied'] == 4
    assert state['intents'] == audit_intents
    state = execution.lp_auto_run_once(round_id=f'known-idless-extra-{side}')
    assert 'unbounded_financial_uncertainty' in state['admission_block_reasons']
    assert state['funds']['spendable_usd'] is None
    assert account.posts == account.cancels == []
    assert state['intents'] == audit_intents
    assert store.lp_actions(original['session_id']) == audit_actions


def test_presend_account_failure_cause_survives_a_different_final_refresh_failure(runtime):
    from open_trader.polymarket_trading import TradingConfig
    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24'}
    _, adapter, account, _, execution, _ = prepare(runtime, count=2, target=2, public=public)
    original_sign, positions = account.create_limit_order, account.list_positions
    reads = []
    signs = []
    def read_positions(**kwargs):
        reads.append(True)
        if len(reads) == 1:
            raise TimeoutError('offline initial presend account failure')
        # The final read is allowed to update the UI with a different failure.
        adapter.config = TradingConfig('0x' + 'b' * 40, '0x' + 'b' * 40)
        return positions(**kwargs)
    def sign(**kwargs):
        signs.append(kwargs['token_id'])
        signed = original_sign(**kwargs)
        account.list_positions = read_positions
        return signed
    account.create_limit_order = sign
    state = execution.lp_auto_run_once(round_id='latched-account-failure')
    assert len(reads) == 2, 'Only failed presend and existing final account reads'
    assert signs == [_refill_identity(1)[2]]
    assert account.posts == account.cancels == []
    assert state['last_round']['reason'] == 'account_order_sync_unknown'
    assert state['last_round']['actions'][0]['state'] == 'entry_rejected'
    assert state['last_round']['actions'][0]['reason'] == 'account_order_sync_unknown'
    assert len(state['last_round']['actions']) == 1
    assert 'account_identity_mismatch' in state['admission_block_reasons']
    assert state['slots']['occupied'] == 0
