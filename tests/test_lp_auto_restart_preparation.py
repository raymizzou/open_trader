"""A dead pre-POST owner must yield its temporary hold to fresh account facts."""

from tests.test_lp_account_reservation_reconciliation import advance_api_wait
import multiprocessing
import faulthandler
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from threading import Event

import pytest
from polymarket.models.clob import SignedOrder

from open_trader.prediction_runtime import PredictionRuntime, PredictionRuntimeOwnershipError, _RuntimeOwnershipLock
from tests.test_lp_account_reservation_reconciliation import runtime, _advance, _refill_identity
from tests.test_lp_auto_refill_contract import prepare, RefillPublic
from tests.test_lp_order_registration_contract import WALLET, _open_order


def _four_buys():
    return tuple(_open_order(f'manual-{i}', 'BUY', price='.40', original='20',
        token_id=_refill_identity(i)[2]).model_copy(update={
            'market': _refill_identity(i)[1], 'condition_id': _refill_identity(i)[1]})
        for i in range(1, 5))


def _old_preparing_owner(path, ready, release, phase):
    # Spawn has no inherited file descriptors, SQLite connections or clock patches.
    diagnostic = (Path(path) / 'preparation-watchdog.log').open('w')
    faulthandler.dump_traceback_later(9, file=diagnostic)
    patch = pytest.MonkeyPatch()
    fixture = runtime.__wrapped__(Path(path), patch)
    build = next(fixture)
    lock = _RuntimeOwnershipLock(Path(path) / 'state.sqlite/prediction_arbitrage/runtime.lock')
    lock.acquire()
    try:
        public = RefillPublic(build.clock)
        public.prices[5] = '.89'
        store, _, account, lp, execution, _ = prepare(build, public=public)
        account.orders = _four_buys()
        sign = account.create_limit_order

        def checkpoint():
            intent = execution.lp_auto_state()['intents'][0]
            faulthandler.cancel_dump_traceback_later()
            ready.send((intent, build.clock[0]))
            assert release.wait(10), 'Independent old-process signing watchdog'

        def blocked(**kwargs):
            checkpoint()
            return sign(**kwargs)
        if phase == 'signing':
            account.create_limit_order = blocked
        elif phase == 'before-post-marker':
            post = lp._post_limit
            def before_post_marker(signed, **kwargs):
                # _submit_prepared has persisted intent=sending, but the
                # durable POST marker has not yet been invoked.
                checkpoint()
                return post(signed, **kwargs)
            lp._post_limit = before_post_marker
        else:
            post = account.post_order
            def awaiting_receipt(signed):
                checkpoint()
                return post(signed)
            account.post_order = awaiting_receipt
        execution.lp_auto_run_once(round_id='old-preparing-request')
    finally:
        faulthandler.cancel_dump_traceback_later()
        diagnostic.close()
        lock.release()
        fixture.close()
        patch.undo()


@pytest.fixture
def dead_preparation(tmp_path, runtime, request):
    context = multiprocessing.get_context('spawn')
    read, write = context.Pipe(duplex=False)
    release = context.Event()
    child = context.Process(target=_old_preparing_owner,
        args=(str(tmp_path), write, release, getattr(request, 'param', 'signing')))
    child.start()
    write.close()
    try:
        diagnostic = tmp_path / 'preparation-watchdog.log'
        assert read.poll(10), ('Independent preparation registration watchdog; '
            f'alive={child.is_alive()} exitcode={child.exitcode}; '
            + (diagnostic.read_text() if diagnostic.exists() else 'child did not enter preparation'))
        intent, boundary = read.recv()
        runtime.clock[0] = boundary + timedelta(seconds=1)
        yield child, intent, release
    finally:
        # Cleanup uses real process supervision; the business clock cannot hide a leak.
        if child.is_alive():
            child.terminate()
        child.join(5)
        if child.is_alive():
            child.kill()
            child.join(5)
        read.close()
        assert not child.is_alive()


def _new_owner(runtime, monkeypatch, tmp_path, *, orders=None):
    import open_trader.prediction_runtime as module
    public = RefillPublic(runtime.clock)
    store, adapter, account, _, _ = runtime(public_client=public, orders=_four_buys() if orders is None else orders)
    monkeypatch.setattr(module, 'load_trading_config', lambda _: adapter.config)
    monkeypatch.setattr(module.PolymarketTradingClient, 'from_keychain', lambda _: adapter)
    monkeypatch.setattr(module, 'PolymarketMonitor', lambda **_: SimpleNamespace(stop=lambda: None))
    def legacy_startup(execution):
        # This scenario concerns LP recovery, not the unrelated legacy venue gate.
        execution._breaker_open = False
        return {'state': 'ready'}
    monkeypatch.setattr(module.PredictionExecutionService, 'reconcile_startup', legacy_startup)
    for name in ('_start_lp_monitor', '_start_lp_auto_monitor', '_start_lp_daily_report_monitor',
        '_start_history_monitor', '_start_candidate_scan_monitor', '_start_candidate_maintenance_monitor',
        '_start_candidate_competition_monitor', '_start_lp_dashboard_monitor', '_start_reward_monitor',
        '_start_lp_share_watch'):
        monkeypatch.setattr(PredictionRuntime, name, lambda self: None)
    owner = PredictionRuntime(data_dir=store.data_dir, prediction_config_path=tmp_path/'offline.json',
        dashboard_url='http://offline.invalid/', n_leg_paused=True,
        history_clock=lambda: runtime.clock[0])
    return owner, store, adapter, account, public


def _terminate_old(child):
    child.terminate()
    child.join(5)
    assert not child.is_alive() and child.exitcode != 0


def _arm_refill(runtime, owner, account, *, index=5):
    market, condition, token = _refill_identity(index)
    lp = owner.lp
    facts = lp._read_candidate_facts(dict(market_id=market, condition_id=condition, token_id=token, outcome='YES'))
    lp._candidate_pool_record_success(condition, {'condition_id': condition}, judged_at=lp._now(),
        facts={'directions': [facts['direction']], 'account': facts['account']})

    def sign(**kwargs):
        _advance(runtime)
        assert kwargs['token_id'] == token
        return SignedOrder(builder='0x1', expiration=kwargs['expiration'], maker=WALLET,
            maker_amount=int(kwargs['price'] * kwargs['size'] * 1000000), metadata='0x3',
            order_type='GTD', salt=2, side='BUY', signature='0x4', signature_type=0,
            signer=WALLET, taker_amount=int(kwargs['size'] * 1000000), timestamp=2,
            token_id=token, post_only=True)

    def post(signed):
        _advance(runtime)
        account.posts.append(signed)
        account.orders += (_open_order('new-owner-buy', 'BUY', price='.40', original='20', token_id=token)
            .model_copy(update={'market': condition, 'condition_id': condition}),)
        return {'accepted': True, 'order_id': 'new-owner-buy', 'status': 'LIVE', 'size_matched': '0'}

    balance = account.get_balance_allowance
    def current_balance(**kwargs):
        _advance(runtime)
        return balance(**kwargs)
    account.get_balance_allowance = current_balance
    account.create_limit_order = sign
    account.post_order = post


@pytest.mark.parametrize('shape', ['registered', 'observed-review'])
@pytest.mark.parametrize('dead_preparation', ['signing', 'before-post-marker'], indirect=True)
def test_dead_prepost_owner_recovers_with_fresh_api_and_refills_without_replaying_old_request(
        runtime, dead_preparation, monkeypatch, tmp_path, shape):
    child, original, _ = dead_preparation
    owner, store, _, account, _ = _new_owner(runtime, monkeypatch, tmp_path)
    sid = original['session_id']
    audit = deepcopy(store.lp_actions(sid))
    assert Decimal(original['reserved_usd']) == Decimal('17.80')
    assert audit[0]['state'] == 'pending' and audit[0]['post_started'] is False
    assert not store.lp_session(sid).get('submit_post_started_at')
    # A still-live owner excludes startup even though this reader has no send lane.
    with pytest.raises(PredictionRuntimeOwnershipError):
        owner.start()
    assert store.lp_actions(sid) == audit
    _terminate_old(child)
    if shape == 'observed-review':
        # The deployed old row was reconciled to review/UNKNOWN, with no
        # session-level submit fields. Only its exact entry action has them.
        session = store.lp_update_session(sid, state='review')
        _, _, _, _, reader = runtime()
        reader._auto_pool._record_session(original['intent_id'], session)
        assert reader.lp_auto_state()['intents'][0]['state'] == 'unknown'
        assert not any(key in session for key in ('submit_stage', 'post_started', 'submit_requested_at'))
    owner, store, _, account, _ = _new_owner(runtime, monkeypatch, tmp_path)
    owner.start()
    try:
        assert owner.state == 'RUNNING'
        before = owner.execution.lp_auto_state()
        assert before['slots']['occupied'] == 5
        assert not before['intents'][0].get('reservation_coverage')
        _advance(runtime)
        assert owner.execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        state = owner.execution.lp_auto_state()
        assert state['slots']['occupied'] == 4, state
        assert state['funds']['status'] == 'known'
        assert Decimal(state['funds']['buy_reserved_usd']) == 32
        assert store.lp_actions(sid) == audit
        assert state['intents'][0]['intent_id'] == original['intent_id']
        assert not state['intents'][0]['order_id']
        marker = deepcopy(store.lp_session(sid)['submission_owner_exit'])
        advance_api_wait(runtime, owner.execution, refresh=False)
        owner.execution.lp_auto_run_once(round_id='old-preparing-request')
        advance_api_wait(runtime, owner.execution, refresh=False)
        _arm_refill(runtime, owner, account)
        reads = account.position_reads
        state = owner.execution.lp_auto_run_once(round_id='new-owner-refill')
        assert state['slots']['occupied'] == len(account.orders) == 5, state['last_round']
        assert len(account.posts) == 1
        assert account.position_reads > reads, 'Refill still needs mandatory fresh account reads'
        assert state['funds']['status'] == 'known'
        assert next(i for i in state['intents'] if i['intent_id'] == original['intent_id'])['order_id'] is None
        assert any(i['session_id'] != sid and i['order_id'] == 'new-owner-buy' for i in state['intents'])
        owner.execution.lp_auto_reconcile_unknown()
        owner.execution.lp_auto_run_once(round_id='old-preparing-request')
        owner.execution.lp_auto_run_once(round_id='new-owner-refill')
        assert len(account.posts) == 1
        assert store.lp_actions(sid) == audit
        orders = account.orders
    finally:
        owner.stop()
    owner, store, adapter, account, _ = _new_owner(runtime, monkeypatch, tmp_path, orders=orders)
    owner.start()
    try:
        _advance(runtime)
        owner.execution.refresh_lp_dashboard_snapshot()
        assert owner.execution.lp_auto_state()['slots']['occupied'] == 5
        assert store.lp_session(sid)['submission_owner_exit'] == marker
        owner.execution.lp_auto_run_once(round_id='after-second-restart')
        assert account.posts == account.cancels == account.market_orders == []
        assert store.lp_actions(sid) == audit
        # A late current API order is real occupancy, independent of the old
        # request audit. Publish it without running overcapacity cancellation.
        _, condition, token = _refill_identity(5)
        account.orders += (_open_order('late-api-buy', 'BUY', price='.40', original='20', token_id=token)
            .model_copy(update={'market': condition, 'condition_id': condition}),)
        _advance(runtime)
        fresh = adapter.lp_account_snapshot_shared(max_age_seconds=0, trade_generation_provider=store.lp_trade_generation)
        owner.lp.register_account_snapshot(fresh)
        late = owner.execution.lp_auto_state()
        assert late['slots']['occupied'] == 6
        assert Decimal(late['funds']['buy_reserved_usd']) == 48
        assert store.lp_actions(sid) == audit
        assert account.posts == account.cancels == []
    finally:
        owner.stop()


@pytest.mark.parametrize('invalid', ['session-post', 'session-post-time', 'action-unknown',
    'action-token', 'action-key', 'action-no-stage', 'independent-action', 'binding', 'duplicate-binding',
    'intent-order', 'intent-conflict'])
def test_restart_covers_dead_buy_only_with_consistent_identity(
        runtime, dead_preparation, monkeypatch, tmp_path, invalid):
    child, original, _ = dead_preparation
    _terminate_old(child)
    owner, store, _, account, _ = _new_owner(runtime, monkeypatch, tmp_path)
    sid = original['session_id']
    action = store.lp_actions(sid)[0]
    if invalid.startswith('session-post'):
        store.lp_update_session(sid, patch={'post_started': True, 'submit_stage': 'sending'}
            if invalid == 'session-post' else {'submit_post_started_at': runtime.clock[0].isoformat()})
    elif invalid == 'independent-action':
        store.lp_upsert_action(sid, sid+':protected-exit', state='unknown',
            payload={'role': 'protected_exit', 'side': 'SELL', 'token_id': original['token_id']})
    elif invalid in {'binding', 'action-key'}:
        with store._transaction() as tx:
            if invalid == 'binding':
                tx.execute('UPDATE lp_sessions SET idempotency_key=? WHERE session_id=?', ('unproven', sid))
            else:
                tx.execute('UPDATE lp_actions SET action_key=? WHERE action_id=?', (sid+':wrong-entry', action['action_id']))
    elif invalid == 'duplicate-binding':
        _, _, _, _, reader = runtime()
        reader._auto_pool._update(lambda d: d['intents'].update(duplicate={**original, 'intent_id': 'duplicate'}))
    elif invalid.startswith('intent-'):
        _, _, _, _, reader = runtime()
        reader._auto_pool._update(lambda d: d['intents'][original['intent_id']].update(
            {'order_id': 'inconsistent-known-id'} if invalid == 'intent-order'
            else {'order_identity_conflict': {'reason': 'inconsistent_identity'}}))
    else:
        store.lp_upsert_action(sid, action['action_key'], state='unknown' if invalid == 'action-unknown' else 'pending',
            payload={**action, 'token_id': _refill_identity(6)[2] if invalid == 'action-token' else action['token_id'],
                'submit_stage': None if invalid == 'action-no-stage' else action['submit_stage']})
    audit = deepcopy(store.lp_actions(sid))
    owner.start()
    try:
        _advance(runtime)
        owner.execution.refresh_lp_dashboard_snapshot()
        current = owner.execution.lp_auto_state()
        covered = invalid in {'session-post', 'session-post-time', 'action-unknown', 'action-no-stage', 'independent-action'}
        # POST/pending labels after confirmed owner exit now use API authority;
        # conflicting identities and independent unproven actions still block.
        assert bool(store.lp_session(sid).get('reservation_coverage')) is covered
        assert current['slots']['occupied'] == 4 if covered else current['slots']['occupied'] >= 5
        if invalid == 'independent-action':
            # API covers the original entry. The UNKNOWN SELL adds no BUY
            # hold; four real BUYs reserve 32 from the fixed budget of 100.
            assert current['funds']['status'] == 'known'
            assert Decimal(current['funds']['buy_reserved_usd']) == 32
            assert Decimal(current['funds']['spendable_usd']) == 68
            assert Decimal(current['funds']['inventory_cost_usd']) == 0
            assert not current['admission_block_reasons']
        assert store.lp_actions(sid) == audit
        assert account.posts == account.cancels == []
    finally:
        owner.stop()


@pytest.mark.parametrize('invalid', ['incomplete-orders', 'incomplete-positions', 'stale',
    'pre-recovery', 'generation', 'account', 'session-account', 'late-action', 'late-action-only',
    'changed-intent', 'changed-intent-order', 'changed-intent-conflict', 'changed-session-token',
    'changed-session-binding'])
def test_recovered_preparation_still_requires_valid_unchanged_fresh_account_publication(
        runtime, dead_preparation, monkeypatch, tmp_path, invalid):
    child, original, _ = dead_preparation
    _terminate_old(child)
    owner, store, adapter, account, _ = _new_owner(runtime, monkeypatch, tmp_path)
    owner.start()
    sid = original['session_id']
    try:
        marker = next(iter(store.lp_session(sid)['submission_owner_exit'].values()))
        # Match the observed review/UNKNOWN residue before invalid publication.
        session = store.lp_update_session(sid, state='review')
        owner.execution._auto_pool._record_session(original['intent_id'], session)
        _advance(runtime)
        snapshot = adapter.lp_account_snapshot_shared(max_age_seconds=0,
            trade_generation_provider=store.lp_trade_generation)
        if invalid.startswith('incomplete-'):
            snapshot['open_orders_complete' if invalid == 'incomplete-orders' else 'positions_complete'] = False
        elif invalid == 'stale':
            _advance(runtime, 61)
        elif invalid == 'pre-recovery':
            snapshot['read_started_at'] = marker['ended_at']
        elif invalid == 'generation':
            store.lp_advance_trade_generation(store.lp_trade_generation())
        elif invalid == 'account':
            snapshot['wallet_address'] = '0x'+'b'*40
        elif invalid == 'session-account':
            store.lp_update_session(sid, patch={'account_id': '0x'+'b'*40})
        elif invalid.startswith('changed-intent'):
            update = ({'token_id': _refill_identity(6)[2]} if invalid == 'changed-intent'
                else {'order_id': 'inconsistent-known-id'} if invalid == 'changed-intent-order'
                else {'order_identity_conflict': {'reason': 'inconsistent_identity'}})
            owner.execution._auto_pool._update(lambda d: d['intents'][original['intent_id']].update(update))
        elif invalid == 'changed-session-token':
            store.lp_update_session(sid, patch={'token_id': _refill_identity(6)[2]})
        elif invalid == 'changed-session-binding':
            with store._transaction() as tx:
                tx.execute('UPDATE lp_sessions SET idempotency_key=? WHERE session_id=?', ('unproven', sid))
        else:
            # A durable receipt/POST arriving after startup invalidates the
            # certified action. The stale pending preparing action cannot win.
            if invalid == 'late-action':
                store.lp_update_session(sid, patch={'post_started': True, 'submit_stage': 'sending'})
            action = store.lp_actions(sid)[0]
            store.lp_upsert_action(sid, action['action_key'], state='unknown' if invalid == 'late-action' else 'pending',
                payload={**action, 'post_started': True, 'submit_stage': 'send_unknown',
                    'submit_finished_at': runtime.clock[0].isoformat()})
        owner.lp.register_account_snapshot(snapshot)
        assert not store.lp_session(sid).get('reservation_coverage')
        state = owner.execution.lp_auto_state()
        assert state['slots']['occupied'] >= 5
        assert state['funds']['status'] == 'unknown'
        assert account.posts == account.cancels == []
    finally:
        owner.stop()


@pytest.mark.parametrize('identity', ['registered-order', 'entry-id', 'owned-ids', 'history'])
def test_api_added_session_identity_does_not_defeat_owner_ended_coverage(
        runtime, dead_preparation, monkeypatch, tmp_path, identity):
    from open_trader.polymarket_lp_accounting import ended_reservation_evidence
    child, original, _ = dead_preparation
    _terminate_old(child)
    owner, store, adapter, account, _ = _new_owner(runtime, monkeypatch, tmp_path)
    owner.start()
    sid = original['session_id']
    try:
        assert store.lp_session(sid)['submission_owner_exit']
        session = store.lp_update_session(sid, state='review')
        owner.execution._auto_pool._record_session(original['intent_id'], session)
        audit = deepcopy(store.lp_actions(sid))
        order = dict(order_id='late-durable-buy', token_id=original['token_id'],
            side='BUY', status='LIVE', price='.89', original_size='20', size_matched='0', quantity='20')
        if identity == 'registered-order':
            store.lp_register_exchange_orders(WALLET, original['token_id'], [order],
                session=store.lp_session(sid), expected_generation=store.lp_trade_generation())
            registered = store.lp_session(sid)
            assert registered['session_id'] == sid
            assert registered['entry_order_id'] == order['order_id']
            assert registered['owned_order_ids'] == [order['order_id']]
            assert order['order_id'] in registered['order_history']
            account.orders += (_open_order(order['order_id'], 'BUY', price='.89', original='20',
                token_id=original['token_id']).model_copy(update={
                    'market': original['condition_id'], 'condition_id': original['condition_id']}),)
        else:
            patch = ({'entry_order_id': order['order_id']} if identity == 'entry-id'
                else {'owned_order_ids': [order['order_id']]} if identity == 'owned-ids'
                else {'order_history': {order['order_id']: order}})
            store.lp_update_session(sid, patch=patch)
        assert store.lp_actions(sid) == audit, 'Identity registration must not mutate the pending action'
        _advance(runtime)
        current = owner.execution._auto_pool._read()
        evidence = ended_reservation_evidence(current['intents'][original['intent_id']],
            store.lp_session(sid), audit, account_id=WALLET,
            pool_account_id=current['account_id'], read_started_at=runtime.clock[0])
        # API registration may add IDs without changing the original pending
        # action. Owner exit supplies ended-send proof independently of IDs.
        assert evidence['basis'] == 'runtime_owner_exit', evidence
        snapshot = adapter.lp_account_snapshot_shared(max_age_seconds=0,
            trade_generation_provider=store.lp_trade_generation)
        owner.lp.register_account_snapshot(snapshot)
        state = owner.execution.lp_auto_state()
        assert store.lp_session(sid).get('reservation_coverage')
        facts = owner.execution._auto_pool._read()['account_financial_facts']
        assert facts['financial_status'] == 'known'
        assert len(facts['buys']) == len(account.orders), 'Current API exposure still counts by actual ID'
        intent = next(i for i in state['intents'] if i['intent_id'] == original['intent_id'])
        assert intent['reserved_usd'] == original['reserved_usd']
        assert intent['order_id'] is None
        assert state['funds']['status'] == 'known'
        assert store.lp_actions(sid) == audit
        assert account.posts == account.cancels == []
    finally:
        owner.stop()


@pytest.mark.parametrize('dead_preparation', ['awaiting-receipt'], indirect=True)
def test_dead_post_started_sender_with_lagging_preparing_action_is_covered_after_owner_exit(
        runtime, dead_preparation, monkeypatch, tmp_path):
    child, original, _ = dead_preparation
    _terminate_old(child)
    owner, store, _, account, _ = _new_owner(runtime, monkeypatch, tmp_path)
    sid = original['session_id']
    audit = deepcopy(store.lp_actions(sid))
    assert audit[0]['state'] == 'pending' and audit[0]['submit_stage'] == 'preparing'
    assert audit[0]['post_started'] is False
    assert store.lp_session(sid)['post_started'] is True
    owner.start()
    try:
        _advance(runtime)
        owner.execution.refresh_lp_dashboard_snapshot()
        state = owner.execution.lp_auto_state()
        # Approved API-authority follow-up: dead local sender is no longer in
        # flight. Its unknown historical venue receipt is not a permanent hold.
        assert state['slots']['occupied'] == 4
        assert state['funds']['status'] == 'known'
        assert store.lp_session(sid).get('reservation_coverage')
        assert store.lp_actions(sid) == audit
        assert account.posts == account.cancels == []
        advance_api_wait(runtime, owner.execution, refresh=False)
        owner.execution.lp_auto_run_once(round_id='old-preparing-request')
        advance_api_wait(runtime, owner.execution, refresh=False)
        _arm_refill(runtime, owner, account)
        state = owner.execution.lp_auto_run_once(round_id='post-owner-exit-refill')
        assert state['slots']['occupied'] == 5
        assert len(account.posts) == 1
        assert store.lp_actions(sid) == audit
    finally:
        owner.stop()


@pytest.mark.parametrize('fence, reason', [('pause', 'manually_paused'), ('config', 'config_version_changed')])
def test_recovery_preserves_new_submission_pause_and_configuration_fences(
        runtime, dead_preparation, monkeypatch, tmp_path, fence, reason):
    child, original, _ = dead_preparation
    _terminate_old(child)
    owner, store, _, account, _ = _new_owner(runtime, monkeypatch, tmp_path)
    owner.start()
    entered, release = Event(), Event()
    try:
        _advance(runtime)
        owner.execution.refresh_lp_dashboard_snapshot()
        assert owner.execution.lp_auto_state()['slots']['occupied'] == 4
        advance_api_wait(runtime, owner.execution, refresh=False)
        owner.execution.lp_auto_run_once(round_id='old-preparing-request')
        advance_api_wait(runtime, owner.execution, refresh=False)
        _arm_refill(runtime, owner, account)
        sign = account.create_limit_order
        def blocked(**kwargs):
            entered.set()
            assert release.wait(5), 'Independent new-owner signing watchdog'
            return sign(**kwargs)
        account.create_limit_order = blocked
        with ThreadPoolExecutor(1) as workers:
            pending = workers.submit(owner.execution.lp_auto_run_once, round_id='fenced-restart-refill')
            try:
                assert entered.wait(5)
                if fence == 'pause':
                    owner.execution.lp_auto_set_desired_running(False)
                else:
                    owner.execution._auto_pool._update(lambda d: d.update(config_version=d['config_version']+1, trading_config_version=d['trading_config_version']+1))
            finally:
                release.set()
            state = pending.result(timeout=5)
        assert state['last_round']['actions'][0]['reason'] == reason
        assert state['slots']['occupied'] == 4
        assert store.lp_session(original['session_id'])['reservation_coverage']['basis'] == 'runtime_owner_exit'
        assert account.posts == account.cancels == []
    finally:
        release.set()
        owner.stop()
