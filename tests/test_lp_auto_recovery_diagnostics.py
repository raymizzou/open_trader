"""Issue #253: recovery and same-round filtering through real offline boundaries."""

from tests.test_lp_account_reservation_reconciliation import advance_api_wait
from tests.test_lp_auto_pool import advance_auto_wait
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import threading
import sys

import pytest
from copy import deepcopy
from datetime import timedelta
from tests import test_lp_auto_pool as pool
from open_trader import polymarket_lp_auto as auto_module
import json
from decimal import Decimal
from tests import test_lp_auto_rotation as rotation
from tests.test_lp_auto_refill_contract import prepare, runtime, _advance, _refill_identity
from tests.test_lp_order_registration_contract import _open_order
from open_trader.polymarket_lp import LpObservationWait

def _exercise_recovered_account(runtime, *, cleanup_release=None):
    store, adapter, account, lp, execution, public=prepare(runtime,count=5,target=5)
    original_orders=[]
    for i in (1,2,3):
        _, condition, token=_refill_identity(i)
        original_orders.append(_open_order(f'existing-{i}', 'BUY', price='.40', original='20',token_id=token)
                               .model_copy(update={'market':condition,'condition_id':condition}))
    account.orders=tuple(original_orders)
    auto=execution._auto_pool
    assert auto._refresh_account_facts() is True
    assert auto.state()['slots']['occupied']==3
    _advance(runtime,61)
    for i in (4,5):
        market,condition,token=_refill_identity(i)
        facts=lp._read_candidate_facts(dict(market_id=market,condition_id=condition,token_id=token,outcome='YES'))
        lp._candidate_pool_record_success(condition,dict(condition_id=condition),judged_at=lp._now(),
                                         facts=dict(directions=[facts['direction']],account=facts['account']))
    real_reader=adapter.lp_account_snapshot_shared
    entered,release=Event(),Event();reads=[];failed_once=False
    def reader(*args,**kwargs):
        nonlocal failed_once
        reads.append(dict(kwargs))
        if not failed_once:
            failed_once = True
            entered.set()
            assert release.wait(5), 'controlled read watchdog'
            raise LpObservationWait('account_round_invalid')
        return real_reader(*args,**kwargs)
    adapter.lp_account_snapshot_shared=reader
    try:
        with ThreadPoolExecutor(1) as workers:
            pending=workers.submit(execution.lp_auto_run_once,round_id='three-to-five-recovery')
            try:
                assert entered.wait(3)
                _advance(runtime)
                snapshot=real_reader(max_age_seconds=0,trade_generation_provider=store.lp_trade_generation)
                assert lp.register_account_snapshot(snapshot)['state']=='registered'
            finally:
                release.set()
            result=pending.result(timeout=5)
        assert account.posts == [] and len(reads) == 1
        assert result['funds']['status'] == 'known'
        assert result['plan_wait']['kind'] == 'api'
        from datetime import datetime
        deadline = datetime.fromisoformat(result['plan_wait']['deadline'])
        assert deadline - datetime.fromisoformat(result['plan_wait']['started_at']) == timedelta(seconds=60)
        runtime.clock[0] = deadline - timedelta(seconds=1)
        early = execution.lp_auto_run_once(round_id='three-to-five-recovery')
        assert len(reads) == 1 and account.posts == []
        assert early['funds']['status'] == 'known'
        advance_api_wait(runtime, execution)
        reads.clear()
        result = execution.lp_auto_run_once(round_id='three-to-five-recovery')
    finally:
        # Executor exit reclaims the controlled worker before attention cleanup.
        release.set()
        failure = sys.exc_info()[1]
        attention=lp._attention_thread
        if cleanup_release is not None:
            cleanup_release.set()
        if attention is not None:
            attention.join(3)
            if attention.is_alive():
                if failure is not None:
                    failure.add_note('attention cleanup watchdog')
                else:
                    raise AssertionError('attention cleanup watchdog')
    assert len(account.posts)==2, 'fresh publication and two fresh candidates must reach mandatory per-send reads: ' + str(result['last_round'])
    assert len(account.orders)==5
    assert result['slots']['occupied']==5
    assert result['admission_block_reasons']==[]
    assert result['funds']['status']=='known'
    assert len(reads) in (6, 7), 'due initial read, four mandatory BUY fences, final refresh and at most one maintenance reuse'
    forced = [read for read in reads if read.get('max_age_seconds') == 0
              and read.get('trade_generation_provider') == store.lp_trade_generation]
    assert len(forced)==6, 'initial/final refresh and both mandatory fresh reads per BUY remain forced'
    assert sum('max_age_seconds' not in read for read in reads) == len(reads) - 6, 'only the existing optional maintenance call'


def test_recovered_account_can_refill_three_to_five(runtime):
    _exercise_recovered_account(runtime)


@pytest.mark.parametrize('outcome', ['wait', 'error', 'registration_wait', 'registration_error'])
def test_current_failed_attempt_discards_its_ranking_account(runtime, outcome):
    store, adapter, _, _, execution, _ = prepare(runtime, count=1, target=1)
    auto = execution._auto_pool
    assert auto._refresh_account_facts() is True
    snapshot = auto._current_account
    if outcome == 'registration_wait':
        store.lp_advance_trade_generation(store.lp_trade_generation())
    elif outcome == 'registration_error':
        snapshot = {**snapshot, 'balance_complete': False}
    def failed(**kwargs):
        if outcome.startswith('registration_'):
            return snapshot
        if outcome == 'wait':
            raise LpObservationWait('account_round_invalid')
        raise TimeoutError('offline account failure')
    adapter.lp_account_snapshot_shared = failed
    assert auto._refresh_account_facts() is False
    assert auto._current_account is None, 'Current failed attempt must discard its ranking cache'


@pytest.mark.parametrize('older_outcome', ['wait', 'error', 'success'])
def test_late_attempt_cannot_replace_or_discard_newer_success(runtime, older_outcome):
    _, adapter, _, _, execution, _ = prepare(runtime, count=1, target=1)
    auto = execution._auto_pool
    assert auto._refresh_account_facts() is True
    old_snapshot = auto._current_account
    real_reader = adapter.lp_account_snapshot_shared
    entered, release = Event(), Event()
    reads = []
    def overlapping(**kwargs):
        index = len(reads)
        reads.append(index)
        if index == 0:
            entered.set()
            assert release.wait(5), 'independent real watchdog'
            if older_outcome == 'wait':
                raise LpObservationWait('session_changed')
            if older_outcome == 'error':
                raise TimeoutError('late offline account failure')
            return old_snapshot
        return real_reader(**kwargs)
    adapter.lp_account_snapshot_shared = overlapping
    with ThreadPoolExecutor(1) as workers:
        old = workers.submit(auto._refresh_account_facts)
        try:
            assert entered.wait(3)
            _advance(runtime)
            assert auto._refresh_account_facts() is True
            current = auto._current_account
            durable = auto._read()['account_financial_facts']
        finally:
            release.set()
        assert old.result(timeout=3) is False
    assert auto._current_account is current
    assert current is not old_snapshot
    assert auto._read()['account_financial_facts'] == durable
    assert auto._account_facts_wait is None
    assert execution._lp._account_order_sync_error is None
    assert not auto.state()['admission_block_reasons']
    assert len(reads) == 2


@pytest.mark.parametrize('boundary', ['no_new_publication', 'generation_changed', 'publication_expired'])
def test_failed_attempt_still_blocks_without_current_durable_facts(runtime, boundary):
    store, adapter, account, lp, execution, _ = prepare(runtime, count=1, target=1)
    auto = execution._auto_pool
    assert auto._refresh_account_facts() is True
    real_reader = adapter.lp_account_snapshot_shared
    initial_facts = deepcopy(auto._read()['account_financial_facts'])
    reads = []
    def invalidated(**kwargs):
        reads.append(True)
        if boundary != 'no_new_publication':
            _advance(runtime)
            fresh = real_reader(**kwargs)
            assert lp.register_account_snapshot(fresh)['state'] == 'registered'
            if boundary == 'generation_changed':
                store.lp_advance_trade_generation(store.lp_trade_generation())
            else:
                _advance(runtime, 61)
        raise LpObservationWait('account_round_invalid')
    adapter.lp_account_snapshot_shared = invalidated
    state = execution.lp_auto_run_once(round_id='counterfactual-' + boundary)
    assert len(reads) == 1
    assert account.posts == account.cancels == []
    assert state['funds']['spendable_usd'] is None
    assert state['funds']['status'] == 'unknown'
    assert 'account_round_invalid' in state['admission_block_reasons']
    if boundary == 'no_new_publication':
        assert auto._read()['account_financial_facts'] == initial_facts
    elif boundary == 'generation_changed':
        assert 'account_financial_facts_changed' in state['admission_block_reasons']
    else:
        assert 'account_financial_facts_stale' in state['admission_block_reasons']


@pytest.mark.parametrize('case, reason, count', [
    ('stale_reward', 'reward_data_stale', 'qualification_unknown'),
    ('missing_facts', 'qualification_facts_missing', 'missing_facts'),
    ('expired_pool', 'candidate_pool_expired', 'pool_expired'),
    ('excluded_market', 'participating_market', 'participating_markets'),
    ('insufficient_allowance', 'balance_insufficient', 'qualification_rejected'),
])
def test_zero_candidate_round_keeps_filter_counts(tmp_path, case, reason, count):
    engine, exchange, lp, store = pool.setup(tmp_path, 1)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=5))
    engine.lp_auto_set_desired_running(True)
    if case == 'stale_reward':
        lp._candidate_qualification_facts['m00']['directions'][0]['reward_checked_at'] = pool.NOW - timedelta(seconds=61)
        # #310 now requalifies before filtering. Keep the source unavailable
        # so this diagnostic case still observes a genuinely stale fact.
        exchange.lp_reward_catalog = lambda **kwargs: dict(state='unknown', markets=[])
    elif case == 'missing_facts':
        lp._candidate_qualification_facts.clear()
    elif case == 'expired_pool':
        lp._candidate_pool['m00']['expires_at'] = pool.NOW.isoformat()
    elif case == 'excluded_market':
        store.lp_create_session('existing', 'existing', state='entry_open', payload=dict(condition_id='m00', token_id='m00'))
    else:
        lp._candidate_qualification_facts['m00']['account']['allowance'] = '0'
    state = engine.lp_auto_run_once(round_id='filter-' + case)
    assert state['last_round']['candidate_count'] == 0
    assert state['last_round']['reason'] == 'candidates_or_funds_insufficient'
    assert exchange.posts == []
    diagnostics = state['last_round']['candidate_filter']
    assert diagnostics['state'] == 'evaluated'
    assert diagnostics['counts']['pool_total'] == 1
    assert diagnostics['counts'][count] == 1
    assert diagnostics['counts']['qualified_directions'] == 0
    assert diagnostics['reasons'] == {reason: 1}
    counts = diagnostics['counts']
    assert counts['pool_total'] == counts['pool_expired'] + counts['pool_unexpired']
    assert counts['pool_unexpired'] == counts['missing_facts'] + counts['facts_present']
    assert counts['facts_present'] == counts['participating_markets'] + counts['evaluated_markets']
    assert counts['evaluated_directions'] == counts['qualification_rejected'] + counts['qualification_unknown'] + counts['eligible_directions']
    assert counts['eligible_directions'] == counts['estimate_unknown'] + counts['qualified_directions']


def test_candidate_diagnostics_keeps_consumed_account_source(tmp_path, monkeypatch):
    engine, exchange, _, _ = pool.setup(tmp_path, 1)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=5))
    engine.lp_auto_set_desired_running(True)
    auto = engine._auto_pool
    auto._current_account = exchange.lp_account_snapshot()
    evaluate = auto_module.evaluate_lp_entry
    def newer_account(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        auto._current_account = {**auto._current_account, 'checked_at': pool.NOW + timedelta(seconds=1)}
        return result
    monkeypatch.setattr(auto_module, 'evaluate_lp_entry', newer_account)
    state = engine.lp_auto_run_once(round_id='account-source')
    diagnostics = state['last_round']['candidate_filter']
    assert diagnostics['account_sources'] == dict(current=1, candidate=0, unknown=0)
    assert diagnostics['facts']['account_at']['min'] == pool.NOW.isoformat()


def test_noncallable_reader_keeps_compatible_current_account(runtime):
    _, adapter, _, _, execution, _ = prepare(runtime, count=1, target=1)
    auto = execution._auto_pool
    assert auto._refresh_account_facts() is True
    current = auto._current_account
    adapter.lp_account_snapshot_shared = None
    assert auto._refresh_account_facts() is None
    assert auto._current_account is current


def test_candidate_diagnostics_uses_consumed_snapshot_under_concurrent_publication(tmp_path, monkeypatch):
    engine, exchange, lp, _ = pool.setup(tmp_path, 1)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=5))
    engine.lp_auto_set_desired_running(True)
    lp._candidate_qualification_facts['m00']['directions'][0]['reward_checked_at'] = pool.NOW - timedelta(seconds=61)
    # This negative snapshot case needs the external reward to remain
    # unavailable after #310's bounded prefilter refresh, not become fresh.
    exchange.lp_reward_catalog = lambda **kwargs: dict(state='unknown', markets=[])
    entered, release = Event(), Event()
    excluded = engine._auto_pool._excluded
    def delayed(condition):
        if not entered.is_set():
            entered.set()
            assert release.wait(5), 'Independent real snapshot watchdog'
        return excluded(condition)
    monkeypatch.setattr(engine._auto_pool, '_excluded', delayed)
    with ThreadPoolExecutor(1) as workers:
        pending = workers.submit(engine.lp_auto_run_once, round_id='snapshot')
        try:
            assert entered.wait(3)
            for token in ('m00', 'm01'):
                lp._candidate_pool_record_success(token, dict(condition_id=token), judged_at=pool.NOW,
                    facts=dict(directions=[exchange.direction(token)], account=exchange.lp_account_snapshot()))
            assert len(engine._auto_pool.candidates()) == 2, 'Concurrent projection consumes newer pool only'
        finally:
            release.set()
        state = pending.result(timeout=5)
    diagnostics = state['last_round']['candidate_filter']
    assert state['last_round']['candidate_count'] == 0
    assert diagnostics['counts']['pool_total'] == diagnostics['counts']['facts_present'] == 1
    assert diagnostics['reasons'] == {'reward_data_stale': 1}
    assert diagnostics['facts']['reward_at']['min'] == (pool.NOW - timedelta(seconds=61)).isoformat()
    assert diagnostics['facts']['pool_checked_at']['min'] == pool.NOW.isoformat()
    assert exchange.posts == []


def test_candidate_diagnostics_does_not_add_reads_clocks_or_re_evaluate(tmp_path, monkeypatch):
    original_evaluate, original_estimate = auto_module.evaluate_lp_entry, auto_module.minimum_order_estimate
    results = []
    for enabled in (False, True):
        engine, exchange, lp, store = pool.setup(tmp_path / str(enabled), 2)
        engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=5))
        engine.lp_auto_set_desired_running(True)
        calls = dict(evaluate=0, estimate=0, db=0, account=0, clock=0)
        def counted(name, function):
            def call(*args, **kwargs):
                calls[name] += 1
                return function(*args, **kwargs)
            return call
        with monkeypatch.context() as patch:
            patch.setattr(auto_module, 'evaluate_lp_entry', counted('evaluate', original_evaluate))
            patch.setattr(auto_module, 'minimum_order_estimate', counted('estimate', original_estimate))
            patch.setattr(store, '_connection', counted('db', store._connection))
            patch.setattr(exchange, 'lp_account_snapshot', counted('account', exchange.lp_account_snapshot))
            patch.setattr(lp, '_now', counted('clock', lp._now))
            patch.setattr(engine._auto_pool, '_submit', lambda *args, **kwargs: ({'state': 'rejected', 'reason': 'strategy_funds_insufficient'}, None))
            if not enabled:
                ranked = engine._auto_pool._ranked_buys
                patch.setattr(engine._auto_pool, '_ranked_buys', lambda state, *, diagnostics=None: ranked(state))
            state = engine.lp_auto_run_once(round_id='counted')
        results.append((calls, state))
    assert results[0][0] == results[1][0]
    assert results[1][0]['evaluate'] == results[1][0]['estimate'] == 4
    assert results[0][1]['last_round']['actions'] == results[1][1]['last_round']['actions']
    diagnostics = results[1][1]['last_round']['candidate_filter']
    assert diagnostics['counts']['qualified_directions'] == 2
    assert diagnostics['facts']['evaluation_used_at']['min'] == pool.NOW.isoformat()
    assert diagnostics['facts']['estimate_used_at']['max'] == pool.NOW.isoformat()


@pytest.mark.parametrize('case', ['paused', 'account', 'mutation_disabled', 'empty'])
def test_candidate_diagnostics_distinguishes_not_evaluated_and_empty(tmp_path, monkeypatch, case):
    engine, exchange, lp, _ = pool.setup(tmp_path, 0 if case == 'empty' else 1)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=5))
    if case != 'paused':
        engine.lp_auto_set_desired_running(True)
    if case == 'account':
        lp._account_order_sync_error = 'account_unknown'
    if case == 'mutation_disabled':
        monkeypatch.setattr(engine, 'lp_mutation_allowed', lambda: False)
    state = engine.lp_auto_run_once(round_id='not-evaluated-' + case)
    diagnostics = state['last_round']['candidate_filter']
    assert diagnostics['round_started_at'] == pool.NOW.isoformat()
    assert 'captured_at' not in diagnostics
    assert diagnostics['state'] == ('evaluated' if case == 'empty' else 'not_evaluated')
    assert diagnostics['counts'] == (dict.fromkeys(diagnostics['counts'], 0) if case == 'empty' else {})
    assert diagnostics['facts'] == {}
    assert exchange.posts == []
    if case == 'account':
        assert state['last_round']['reason'] == 'account_unknown'


def test_candidate_diagnostics_is_bounded_and_has_no_raw_identity(tmp_path, monkeypatch):
    engine, exchange, lp, _ = pool.setup(tmp_path, 40)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=5))
    engine.lp_auto_set_desired_running(True)
    secret = 'credential-wallet-token-order-raw'
    for cached in lp._candidate_qualification_facts.values():
        cached['account']['trade_generation'] = secret
        cached['account']['checked_at'] = secret
    codes = [secret] + sorted(auto_module._CANDIDATE_FILTER_REASONS)
    calls = []
    def reject(*args, **kwargs):
        calls.append(1)
        return dict(state='unknown', reason_codes=[codes[len(calls) - 1]], guidance=None)
    monkeypatch.setattr(auto_module, 'evaluate_lp_entry', reject)
    state = engine.lp_auto_run_once(round_id='bounded')
    diagnostics = state['last_round']['candidate_filter']
    assert len(diagnostics['reasons']) <= 32
    assert 'other' in diagnostics['reasons']
    assert diagnostics['counts']['qualification_unknown'] == 40
    assert diagnostics['facts']['account_generation'] == dict(min=None, max=None, unknown=40)
    serialized = json.dumps(diagnostics)
    assert secret not in serialized and 'test-wallet' not in serialized and 'm00' not in serialized
    assert len(serialized) < 5000
    assert exchange.posts == []


def test_candidate_diagnostics_keeps_unknown_estimate_reason(tmp_path, monkeypatch):
    engine, exchange, _, _ = pool.setup(tmp_path, 1)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=5))
    engine.lp_auto_set_desired_running(True)
    monkeypatch.setattr(auto_module, 'minimum_order_estimate', lambda *args, **kwargs:
                        dict(state='unknown', reason_codes=['book_unknown']))
    state = engine.lp_auto_run_once(round_id='unknown-estimate')
    diagnostics = state['last_round']['candidate_filter']
    assert diagnostics['counts']['eligible_directions'] == diagnostics['counts']['estimate_unknown'] == 1
    assert diagnostics['counts']['qualified_directions'] == state['last_round']['candidate_count'] == 0
    assert diagnostics['reasons'] == {'book_unknown': 1}
    assert exchange.posts == []


@pytest.mark.parametrize('outcome, reason', [('rejected', 'market_not_accepting_orders'), ('error', 'market_read_timeout')])
def test_rotation_recheck_removal_is_distinct_from_initial_qualification(tmp_path, monkeypatch, outcome, reason):
    engine, exchange, lp, _ = rotation.setup(tmp_path, monkeypatch, count=2, target=1)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    rotation.refresh(lp, exchange, 2)
    reader = lp._read_candidate_facts
    def rejected_recheck(row, **kwargs):
        if row['condition_id'] == 'm01' and outcome == 'error':
            raise ValueError('market_read_timeout')
        facts = reader(row, **kwargs)
        if row['condition_id'] == 'm01':
            facts['direction']['market']['accepting_orders'] = False
        return facts
    monkeypatch.setattr(lp, '_read_candidate_facts', rejected_recheck)
    state = engine.lp_auto_run_once(round_id='removed-' + outcome)
    diagnostics = state['last_round']['candidate_filter']
    assert diagnostics['counts']['qualified_directions'] == 1
    assert diagnostics['counts']['recheck_removed'] == 1
    assert state['last_round']['candidate_count'] == 0
    assert diagnostics['recheck_reasons'] == {reason: 1}
    assert state['last_round']['reason'] == 'target_filled'
    assert state['last_round']['actions'] == []
    assert len(exchange.posts) == 1 and exchange.cancels == []


def test_candidate_diagnostics_keeps_original_per_stage_business_times(tmp_path, monkeypatch):
    engine, exchange, lp, _ = pool.setup(tmp_path, 2)
    engine.lp_auto_configure(dict(budget_usd='1', target_buy_count=5))
    engine.lp_auto_set_desired_running(True)
    current = [pool.NOW]
    monkeypatch.setattr(lp, 'clock', lambda: current[0])
    evaluate = auto_module.evaluate_lp_entry
    calls = []
    def advance_between_evaluate_and_estimate(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        calls.append(kwargs['now'])
        if len(calls) == 1:
            current[0] += timedelta(seconds=1)
        return result
    monkeypatch.setattr(auto_module, 'evaluate_lp_entry', advance_between_evaluate_and_estimate)
    state = engine.lp_auto_run_once(round_id='stage-times')
    diagnostics = state['last_round']['candidate_filter']
    assert diagnostics['round_started_at'] == pool.NOW.isoformat()
    assert diagnostics['facts']['pool_checked_at']['min'] == pool.NOW.isoformat()
    assert diagnostics['facts']['evaluation_used_at'] == dict(min=pool.NOW.isoformat(), max=current[0].isoformat(), unknown=0)
    assert diagnostics['facts']['estimate_used_at'] == dict(min=current[0].isoformat(), max=current[0].isoformat(), unknown=0)
    assert state['last_round']['reason'] == 'candidates_or_funds_insufficient'
    assert exchange.posts == []


@pytest.mark.parametrize('case, reason', [
    ('event_starting_soon', 'event_starting_soon'),
    ('event_status_unknown', 'event_status_unknown'),
    ('event_timing_unknown', 'event_timing_unknown'),
    ('stress_loss_exceeded', 'stress_loss_exceeded'),
    ('stress_boundary', None),
    ('book_time_invalid', 'book_freshness_invalid'),
    ('account_time_invalid', 'account_freshness_invalid'),
    ('unknown_external', 'other'),
])
def test_known_candidate_filter_reasons_survive_real_evaluate(tmp_path, case, reason):
    engine, exchange, lp, _ = pool.setup(tmp_path, 1)
    engine.lp_auto_configure(dict(budget_usd='1', target_buy_count=5))
    engine.lp_auto_set_desired_running(True)
    cached = lp._candidate_qualification_facts['m00']
    direction, account = cached['directions'][0], cached['account']
    secret = 'https://private.invalid/wallet-token-credential'
    if case == 'event_starting_soon':
        direction['market']['event_start_time'] = pool.NOW + timedelta(minutes=15)
    elif case == 'event_status_unknown':
        direction['market']['event_ended'] = None
    elif case == 'event_timing_unknown':
        direction['market']['event_start_time'] = 'invalid-event-time'
    elif case in ('stress_loss_exceeded', 'stress_boundary'):
        direction['book']['bids'][1]['price'] = '.35' if case == 'stress_loss_exceeded' else '.36'
    elif case == 'book_time_invalid':
        direction['book']['received_at'] = None
        # A successful source read repairs the missing stamp before ranking;
        # this negative diagnostic needs the external book to remain unknown.
        exchange.lp_order_books = lambda ids, **kwargs: {}
    elif case == 'account_time_invalid':
        account['checked_at'] = None
    else:
        direction['market'].update(event_ended=True, event_finished_at=pool.NOW - timedelta(hours=2))
        direction['screening'] = dict(state='unknown', reason_codes=[secret])
    evaluated = auto_module.evaluate_lp_entry(direction, account=account, now=pool.NOW, candidate=True)
    if reason is None:
        assert evaluated['state'] == 'eligible'
        assert evaluated['guidance']['estimated_exit_loss_ratio'] == Decimal('.10')
    else:
        assert evaluated['state'] != 'eligible'
        assert evaluated['reason_codes'] == [secret if case == 'unknown_external' else reason]
    state = engine.lp_auto_run_once(round_id='real-code-' + case)
    diagnostics = state['last_round']['candidate_filter']
    assert diagnostics['reasons'] == ({} if reason is None else {reason: 1})
    assert diagnostics['counts']['qualified_directions'] == int(reason is None)
    assert secret not in json.dumps(diagnostics)
    assert exchange.posts == []


def test_recovery_probe_failure_preserves_error_and_reclaims_real_threads(runtime, monkeypatch):
    cleanup_release, attention_started = Event(), Event()
    attention_threads, worker_threads = [], []
    real_prepare = prepare
    def failure_runtime(*args, **kwargs):
        result = real_prepare(*args, **kwargs)
        lp, execution = result[3], result[4]
        verifier, run = lp._facts_attention_verifier, execution.lp_auto_run_once
        def verify(*, session_id=None, apply_lock=None):
            if session_id == 'cleanup-probe':
                attention_threads.append(threading.current_thread())
                attention_started.set()
                assert cleanup_release.wait(5), 'Independent attention failure watchdog'
            else:
                return verifier(session_id=session_id, apply_lock=apply_lock)
        def failed_after_round(**kw):
            worker_threads.append(threading.current_thread())
            run(**kw)
            lp._schedule_session_attention('cleanup-probe')
            assert attention_started.wait(3)
            raise RuntimeError('synthetic worker failure after normal round')
        monkeypatch.setattr(lp, '_facts_attention_verifier', verify)
        monkeypatch.setattr(execution, 'lp_auto_run_once', failed_after_round)
        return result
    monkeypatch.setattr(sys.modules[__name__], 'prepare', failure_runtime)
    try:
        with pytest.raises(RuntimeError, match='synthetic worker failure after normal round'):
            _exercise_recovered_account(runtime, cleanup_release=cleanup_release)
        assert attention_started.is_set()
        assert worker_threads and all(not t.is_alive() for t in worker_threads)
        assert attention_threads and all(not t.is_alive() for t in attention_threads)
    finally:
        cleanup_release.set()
        for thread in attention_threads:
            thread.join(3)
            assert not thread.is_alive(), 'Failure-evidence cleanup watchdog'


@pytest.mark.parametrize('reason', [
    'history_latest_refresh_failed', 'history_time_unknown', 'history_amplitude_unknown',
    'ranking_freshness_invalid', 'ranking_freshness_stale',
])
def test_rotation_recheck_keeps_real_history_and_freshness_reasons(tmp_path, monkeypatch, reason):
    engine, exchange, lp, store = rotation.setup(tmp_path, monkeypatch, count=2, target=1)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    rotation.refresh(lp, exchange, 2)
    if reason.startswith('history_'):
        summary = dict(state='known', amplitude='.005', checked_at=pool.NOW,
                       valid_until=pool.NOW + timedelta(days=1))
        if reason == 'history_latest_refresh_failed':
            summary['last_error'] = 'private external refresh error'
        elif reason == 'history_time_unknown':
            summary['last_attempt_at'] = 'invalid-history-attempt-time'
        else:
            summary['amplitude'] = None
        store.lp_save_price_history('m01', 'm01', [], summary)
    else:
        # Corrupt only the consumed live account after its real evaluation and
        # estimate. The original ranking freshness check must reject it.
        read, estimate = lp._read_candidate_facts, auto_module.minimum_order_estimate
        consumed = []
        def capture(row, **kwargs):
            facts = read(row, **kwargs)
            if row['condition_id'] == 'm01':
                consumed.append(facts['account'])
            return facts
        def expire_after_estimate(*args, **kwargs):
            result = estimate(*args, **kwargs)
            if consumed:
                consumed[-1]['checked_at'] = (None if reason == 'ranking_freshness_invalid'
                                            else pool.NOW - timedelta(seconds=61))
            return result
        monkeypatch.setattr(lp, '_read_candidate_facts', capture)
        monkeypatch.setattr(auto_module, 'minimum_order_estimate', expire_after_estimate)
    state = engine.lp_auto_run_once(round_id='real-recheck-' + reason)
    diagnostics = state['last_round']['candidate_filter']
    assert diagnostics['counts']['qualified_directions'] == 1
    assert diagnostics['counts']['recheck_removed'] == 1
    assert diagnostics['recheck_reasons'] == {reason: 1}
    assert state['last_round']['candidate_count'] == 0
    assert state['last_round']['reason'] == 'target_filled'
    assert state['last_round']['actions'] == []
    assert len(exchange.posts) == 1 and exchange.cancels == []
    assert 'private external refresh error' not in json.dumps(diagnostics)
