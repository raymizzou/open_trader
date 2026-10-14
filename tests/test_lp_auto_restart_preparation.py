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
    import json
    import io
    from datetime import datetime
    from dataclasses import asdict, is_dataclass
    evidence = dict(preparations=[], states=[])
    def prepare_public(owner, adapter, public):
        current_rewards = public.list_current_rewards
        def catalog(*, sponsored=False):
            rows = current_rewards(sponsored=sponsored)
            class Pages:
                def iter_items(self): yield from rows
                def first_page(self): return SimpleNamespace(items=rows, has_more=False)
            return Pages()
        monkeypatch.setattr(public,'list_current_rewards',catalog)
        def response(request, **kwargs):
            if request.data:
                body = json.loads(request.data)
                payload = {'history': {token:[dict(t=body['start_ts'],p='.40'),
                    dict(t=body['end_ts'],p='.401')] for token in body['markets']}}
            else:
                payload = {'data':[dict(condition_id=_refill_identity(i)[1],market_competitiveness='2.5')
                    for i in range(1,7)],'next_cursor':'LTE='}
            return io.BytesIO(json.dumps(payload).encode())
        monkeypatch.setattr(adapter,'_urlopen_fn',response)
        competition = owner.lp.refresh_competition_cache()
        prepared = owner.lp.refresh_price_history()
        qualified = owner.lp.refresh_candidates(force=True)
        evidence['preparations'].append(deepcopy(dict(competition=competition,prepared=prepared,qualified=qualified)))
        assert prepared['preparation_outcome'] == 'success', prepared
        row, = [x for x in qualified['candidates'] if x['condition_id'] == _refill_identity(5)[1]]
        assert row['selected_direction']['token_id'] == _refill_identity(5)[2] and row['selected_direction']['eligible'] is True, row
    def persist(label):
        def serialize(value):
            if is_dataclass(value): return asdict(value)
            if hasattr(value,'model_dump'): return value.model_dump(mode='json')
            return str(value)
        (store.path.parent/('CI-F-'+label+'-native-proof.json')).write_text(json.dumps(dict(
            evidence=evidence,current=owner.execution.lp_auto_state(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            posts=account.posts,cancels=account.cancels,orders=account.orders),default=serialize,indent=2))
    child, original, _ = dead_preparation
    owner, store, adapter, account, public = _new_owner(runtime, monkeypatch, tmp_path)
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
        # Observe the ended current sender in review while retaining its honest
        # durable never-POST markers on both original carriers.
        session = store.lp_update_session(sid, state='review')
        _, _, _, _, reader = runtime()
        reader._auto_pool._record_session(original['intent_id'], session)
        assert reader.lp_auto_state()['intents'][0]['state'] == 'unknown'
        assert session['submit_stage'] == 'preparing' and session['post_started'] is False
        assert audit[0]['submit_stage'] == 'preparing' and audit[0]['post_started'] is False
    owner, store, adapter, account, public = _new_owner(runtime, monkeypatch, tmp_path)
    owner.start()
    try:
        assert owner.state == 'RUNNING'
        original_plan = deepcopy(owner.execution.lp_auto_state()['active_plan'])
        original_action = next(a for a in original_plan['actions'] if a['kind'] == 'buy')
        original_terms = [(t['token_id'], t['price'], t['quantity']) for t in original_plan['targets']]
        original_target = next(t for t in original_plan['targets'] if t['token_id'] == original['token_id'])
        assert Decimal(original_target['price']) == Decimal('.89') and Decimal(original_target['quantity']) == 20
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
        original_financial_as_of = state['funds']['as_of']
        advance_api_wait(runtime, owner.execution, refresh=False)
        resolved = owner.execution.lp_auto_run_once(round_id='old-preparing-request')
        evidence['states'].append(deepcopy(dict(label='first-current-cleanup',state=resolved,original_plan=original_plan)))
        assert resolved['last_round']['completed_at'] and resolved['active_plan'] is None, resolved['last_round']
        assert not store.lp_session(sid).get('entry_order_id')
        final_audit = deepcopy(store.lp_actions(sid))
        assert len(final_audit) == len(audit) == 1
        assert final_audit[0]['state'] == 'rejected' and final_audit[0]['reason'] == 'preparation_interrupted'
        assert final_audit[0]['post_started'] is False
        for key in ('action_id', 'action_key', 'created_at', 'token_id', 'side', 'price', 'quantity'):
            assert final_audit[0].get(key) == audit[0].get(key)
        assert account.posts == account.cancels == []
        assert datetime.fromisoformat(resolved['funds']['as_of']) >= datetime.fromisoformat(original_financial_as_of)
        from datetime import datetime
        completed = datetime.fromisoformat(resolved['last_round']['completed_at'])

        assert resolved['plan_wait'] is None
        owner.execution.lp_auto_set_desired_running(False)
        current = owner.execution.lp_auto_run_once(round_id='paused-owner-current')
        assert current['last_round']['round_id'] != resolved['last_round']['round_id']
        assert current['active_plan'] is None and current['last_round']['actions'] == []
        assert account.posts == account.cancels == [] and store.lp_actions(sid) == final_audit
        owner.execution.lp_auto_set_desired_running(True)
        _arm_refill(runtime, owner, account)
        prepare_public(owner, adapter, public)
        reads, planning_as_of = account.position_reads, []
        sign = account.create_limit_order
        def new_sign(**kwargs):
            planning_as_of.append(owner.execution.lp_auto_state()['funds']['as_of'])
            assert kwargs['price'] == Decimal('.40') and kwargs['size'] == 20
            return sign(**kwargs)
        account.create_limit_order = new_sign
        state = owner.execution.lp_auto_run_once(round_id='new-owner-refill')
        assert state['slots']['occupied'] == len(account.orders) == 5, state['last_round']
        assert len(account.posts) == 1
        assert state['last_round']['round_id'] != original_plan['round_id']
        new_action, = [a for a in state['last_round']['actions'] if a['kind'] == 'buy']
        assert new_action['token_id'] == original['token_id'] and new_action['action_id'] != original_action['action_id']
        assert new_action['session_id'] != sid and new_action.get('request_id',new_action['action_id']) != original_action.get('request_id',original_action['action_id'])
        assert store.lp_session(new_action['session_id'])['entry_order_id'] == 'new-owner-buy'
        assert store.lp_session(new_action['session_id'])['idempotency_key'] != store.lp_session(sid)['idempotency_key']
        assert new_action['state'] == 'success' and state['active_plan'] is None
        assert state['last_round']['completed_at']
        assert account.position_reads > reads, 'A new plan still requires fresh complete account publication'
        assert Decimal(state['funds']['buy_reserved_usd']) == 40
        assert planning_as_of and planning_as_of[0]
        assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == planning_as_of[0]
        assert next(i for i in state['intents'] if i['intent_id'] == original['intent_id'])['order_id'] is None
        assert any(i['session_id'] != sid and i['order_id'] == 'new-owner-buy' for i in state['intents'])
        owner.execution.lp_auto_reconcile_unknown()
        owner.execution.lp_auto_run_once(round_id='old-preparing-request')
        owner.execution.lp_auto_run_once(round_id='new-owner-refill')
        assert len(account.posts) == 1 and store.lp_actions(sid) == final_audit
        orders = account.orders
    finally:
        persist('prepost')
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
        assert store.lp_actions(sid) == final_audit
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
        assert store.lp_actions(sid) == final_audit
        assert account.posts == account.cancels == []
    finally:
        persist('prepost')
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
    import json
    import io
    from datetime import datetime
    from dataclasses import asdict, is_dataclass
    evidence = dict(preparations=[], states=[])
    def prepare_public(owner, adapter, public):
        current_rewards = public.list_current_rewards
        def catalog(*, sponsored=False):
            rows = current_rewards(sponsored=sponsored)
            class Pages:
                def iter_items(self): yield from rows
                def first_page(self): return SimpleNamespace(items=rows, has_more=False)
            return Pages()
        monkeypatch.setattr(public,'list_current_rewards',catalog)
        def response(request, **kwargs):
            if request.data:
                body = json.loads(request.data)
                payload = {'history': {token:[dict(t=body['start_ts'],p='.40'),
                    dict(t=body['end_ts'],p='.401')] for token in body['markets']}}
            else:
                payload = {'data':[dict(condition_id=_refill_identity(i)[1],market_competitiveness='2.5')
                    for i in range(1,7)],'next_cursor':'LTE='}
            return io.BytesIO(json.dumps(payload).encode())
        monkeypatch.setattr(adapter,'_urlopen_fn',response)
        competition = owner.lp.refresh_competition_cache()
        prepared = owner.lp.refresh_price_history()
        qualified = owner.lp.refresh_candidates(force=True)
        evidence['preparations'].append(deepcopy(dict(competition=competition,prepared=prepared,qualified=qualified)))
        assert prepared['preparation_outcome'] == 'success', prepared
        row, = [x for x in qualified['candidates'] if x['condition_id'] == _refill_identity(5)[1]]
        assert row['selected_direction']['token_id'] == _refill_identity(5)[2] and row['selected_direction']['eligible'] is True, row
    def persist(label):
        def serialize(value):
            if is_dataclass(value): return asdict(value)
            if hasattr(value,'model_dump'): return value.model_dump(mode='json')
            return str(value)
        (store.path.parent/('CI-F-'+label+'-native-proof.json')).write_text(json.dumps(dict(
            evidence=evidence,current=owner.execution.lp_auto_state(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            posts=account.posts,cancels=account.cancels,orders=account.orders),default=serialize,indent=2))
    child, original, _ = dead_preparation
    _terminate_old(child)
    owner, store, adapter, account, public = _new_owner(runtime, monkeypatch, tmp_path)
    sid = original['session_id']
    audit = deepcopy(store.lp_actions(sid))
    assert audit[0]['state'] == 'pending' and audit[0]['submit_stage'] == 'preparing'
    assert audit[0]['post_started'] is False
    assert store.lp_session(sid)['post_started'] is True
    owner.start()
    try:
        original_plan = deepcopy(owner.execution.lp_auto_state()['active_plan'])
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
        prepare_public(owner, adapter, public)
        state = owner.execution.lp_auto_run_once(round_id='post-owner-exit-refill')
        evidence['states'].append(deepcopy(dict(label='new-current-refill',state=state,original_plan=original_plan)))
        assert state['slots']['occupied'] == len(account.orders) == 5
        assert Decimal(state['funds']['buy_reserved_usd']) == 40
        assert state['active_plan'] is None and state['last_round']['completed_at']

        action, = [a for a in state['last_round']['actions'] if a['kind'] == 'buy']
        old_action, = [a for a in original_plan['actions'] if a['kind'] == 'buy']
        assert action['state'] == 'success' and action['token_id'] == original['token_id']
        assert state['last_round']['round_id'] != original_plan['round_id']
        assert action['action_id'] != old_action['action_id'] and action['session_id'] != sid
        assert action.get('request_id',action['action_id']) != old_action.get('request_id',old_action['action_id'])
        new_session = store.lp_session(action['session_id'])
        assert new_session['idempotency_key'] != store.lp_session(sid)['idempotency_key']
        assert new_session['entry_order_id'] == 'new-owner-buy' and not store.lp_session(sid).get('entry_order_id')
        assert store.lp_session(sid)['post_started'] is True
        assert store.lp_session(sid)['reservation_coverage']['basis'] == 'runtime_owner_exit'
        assert len(account.posts) == 1 and account.posts[0].token_id == original['token_id']
        assert account.posts[0].maker_amount == 8000000 and account.posts[0].taker_amount == 20000000 and account.posts[0].post_only is True
        assert account.cancels == account.market_orders == [] and store.lp_actions(sid) == audit
        completed = datetime.fromisoformat(state['last_round']['completed_at'])
        for elapsed in (1,29):
            owner.execution.lp_auto_run_once(round_id='old-preparing-request')
            owner.execution.lp_auto_run_once(round_id='post-owner-exit-refill')
            assert len(account.posts) == 1 and store.lp_actions(sid) == audit
    finally:
        persist('awaiting-receipt')
        owner.stop()


@pytest.mark.parametrize('fence, reason', [('pause', 'manually_paused'), ('config', 'config_version_changed')])
def test_recovery_preserves_new_submission_pause_and_configuration_fences(
        runtime, dead_preparation, monkeypatch, tmp_path, fence, reason):
    import json
    import io
    from datetime import datetime
    from dataclasses import asdict, is_dataclass
    evidence = dict(preparations=[], states=[])
    def prepare_public(owner, adapter, public):
        current_rewards = public.list_current_rewards
        def catalog(*, sponsored=False):
            rows = current_rewards(sponsored=sponsored)
            class Pages:
                def iter_items(self): yield from rows
                def first_page(self): return SimpleNamespace(items=rows, has_more=False)
            return Pages()
        monkeypatch.setattr(public,'list_current_rewards',catalog)
        def response(request, **kwargs):
            if request.data:
                body = json.loads(request.data)
                payload = {'history': {token:[dict(t=body['start_ts'],p='.40'),
                    dict(t=body['end_ts'],p='.401')] for token in body['markets']}}
            else:
                payload = {'data':[dict(condition_id=_refill_identity(i)[1],market_competitiveness='2.5')
                    for i in range(1,7)],'next_cursor':'LTE='}
            return io.BytesIO(json.dumps(payload).encode())
        monkeypatch.setattr(adapter,'_urlopen_fn',response)
        competition = owner.lp.refresh_competition_cache()
        prepared = owner.lp.refresh_price_history()
        qualified = owner.lp.refresh_candidates(force=True)
        evidence['preparations'].append(deepcopy(dict(competition=competition,prepared=prepared,qualified=qualified)))
        assert prepared['preparation_outcome'] == 'success', prepared
        row, = [x for x in qualified['candidates'] if x['condition_id'] == _refill_identity(5)[1]]
        assert row['selected_direction']['token_id'] == _refill_identity(5)[2] and row['selected_direction']['eligible'] is True, row
    def persist(label):
        def serialize(value):
            if is_dataclass(value): return asdict(value)
            if hasattr(value,'model_dump'): return value.model_dump(mode='json')
            return str(value)
        (store.path.parent/('CI-F-'+label+'-native-proof.json')).write_text(json.dumps(dict(
            evidence=evidence,current=owner.execution.lp_auto_state(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            posts=account.posts,cancels=account.cancels,orders=account.orders),default=serialize,indent=2))
    from time import monotonic
    child, original, _ = dead_preparation
    _terminate_old(child)
    owner, store, adapter, account, public = _new_owner(runtime, monkeypatch, tmp_path)
    old_audit = deepcopy(store.lp_actions(original['session_id']))
    owner.start()
    entered, release = Event(), Event()
    try:
        _advance(runtime)
        owner.execution.refresh_lp_dashboard_snapshot()
        assert owner.execution.lp_auto_state()['slots']['occupied'] == 4
        advance_api_wait(runtime, owner.execution, refresh=False)
        completed = owner.execution.lp_auto_run_once(round_id='old-preparing-request')
        assert completed['last_round']['completed_at'] and completed['active_plan'] is None
        final_audit = deepcopy(store.lp_actions(original['session_id']))
        assert final_audit[0]['state'] == 'rejected' and final_audit[0]['reason'] == 'preparation_interrupted'
        for key in ('action_id','action_key','created_at','token_id','side','price','quantity'):
            assert final_audit[0].get(key) == old_audit[0].get(key)
        assert not store.lp_session(original['session_id']).get('entry_order_id')
        assert account.posts == account.cancels == []
        from datetime import datetime

        advance_api_wait(runtime, owner.execution, refresh=False)
        _arm_refill(runtime, owner, account)
        prepare_public(owner, adapter, public)
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
                held = deepcopy(owner.execution.lp_auto_state())
                evidence['states'].append(deepcopy(dict(label='held-new-current-before-control',state=held)))
                persist('control-held')
                new_action, = [a for a in held['last_round']['actions'] if a['kind'] == 'buy']
                new_intent, = [i for i in held['intents'] if i['intent_id'] == new_action.get('request_id',new_action['action_id'])]
                assert new_intent['state'] == 'reserved' and new_intent['token_id'] == new_action['token_id']
                assert Decimal(new_intent['reserved_usd']) == 8
                new_sid = new_intent['session_id']
                new_audit = deepcopy(store.lp_actions(new_sid))
                assert new_sid != original['session_id'] and new_action['token_id'] == original['token_id']
                assert owner.lp.entry_send_inflight(new_sid)
                if fence == 'pause':
                    owner.execution.lp_auto_set_desired_running(False)
                    deadline = monotonic()+5
                    while True:
                        local = owner.execution.lp_auto_state()
                        if local['active_plan'] is None and local['last_round']['completed_at']:
                            break
                        assert monotonic() < deadline, 'Public new paused local closure watchdog'
                        Event().wait(.01)
                    action, = local['last_round']['actions']
                    assert action['state'] == 'not_sent' and action['reason'] == 'cycle_ended'
                    assert owner.lp.entry_send_inflight(new_sid) and account.posts == []
                else:
                    owner.execution._auto_pool._update(lambda d: d.update(config_version=d['config_version']+1, trading_config_version=d['trading_config_version']+1))
            finally:
                release.set()
            state = pending.result(timeout=5)
            evidence['states'].append(deepcopy(dict(label='historical-local-return',state=state)))
            deadline = monotonic()+5
            while True:
                terminal = owner.execution.lp_auto_state()
                published, = [i for i in terminal['intents'] if i['intent_id'] == new_intent['intent_id']]
                assert published['session_id'] == new_sid and published['token_id'] == new_action['token_id']
                if published['state'] == 'rejected':
                    break
                assert monotonic() < deadline, 'Public same-request terminal publication watchdog'
                Event().wait(.01)
            evidence['states'].append(deepcopy(dict(label='public-same-request-terminal',
                request_id=new_intent['intent_id'],session_id=new_sid,state=terminal)))
            assert not owner.lp.entry_send_inflight(new_sid)
        action, = state['last_round']['actions']
        assert action['reason'] == ('cycle_ended' if fence == 'pause' else 'prepare_failed')
        assert action['state'] == ('not_sent' if fence == 'pause' else 'rejected')
        assert action['action_id'] == new_action['action_id'] and action['session_id'] == new_sid
        assert state['active_plan'] is None and state['last_round']['completed_at']
        audit, = store.lp_actions(new_sid)
        assert audit['action_id'] == new_audit[0]['action_id'] and audit['action_key'] == new_audit[0]['action_key']
        assert audit['state'] == 'rejected' and audit['post_started'] is False
        assert not audit.get('order_id') and not store.lp_session(new_sid).get('entry_order_id')
        assert store.lp_actions(original['session_id']) == final_audit
        if fence == 'pause':
            returned = deepcopy(state)
            state = owner.execution.lp_auto_state()
            evidence['states'].append(deepcopy(dict(label='final-public-after-native-sender',state=state)))
            assert state['last_round']['round_id'] == returned['last_round']['round_id']
            assert state['last_round']['actions'] == returned['last_round']['actions']
            assert state['active_plan'] is None and state['last_round']['completed_at'] == returned['last_round']['completed_at']
            latest_audit, = store.lp_actions(new_sid)
            assert latest_audit == audit and latest_audit['submit_stage'] == 'prepare_failed'
            assert latest_audit['post_started'] is False and not latest_audit.get('order_id')
            assert not store.lp_session(new_sid).get('entry_order_id')
            persist('final-public-assertion-boundary')
            assert Decimal(state['funds']['buy_reserved_usd']) == 32
            assert state['desired_running'] is False and account.market_orders == []
        evidence['states'].append(deepcopy(dict(label='fenced-new-current',held=held,state=state)))
        assert state['slots']['occupied'] == 4
        assert store.lp_session(original['session_id'])['reservation_coverage']['basis'] == 'runtime_owner_exit'
        assert account.posts == account.cancels == []
    finally:
        release.set()
        persist('control')
        owner.stop()
