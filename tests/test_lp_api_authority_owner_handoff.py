"""Current API replaces dead-owner BUY holds without rewriting submit audit."""
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event

import pytest

from open_trader.prediction_runtime import _RuntimeOwnershipLock
from tests.test_lp_account_reservation_reconciliation import runtime, _advance, _refill_identity, _position
from tests.test_lp_auto_refill_contract import prepare, RefillPublic, _historical_api_buy_intents
from tests.test_lp_auto_restart_preparation import _four_buys, _new_owner, _terminate_old, _arm_refill
from tests.test_lp_auto_restart_preparation import dead_preparation
from tests.test_lp_order_registration_contract import _open_order


def _old_buy_owner(path, scenario, ready, release):
    patch = pytest.MonkeyPatch()
    fixture = runtime.__wrapped__(Path(path), patch)
    build = next(fixture)
    lock = _RuntimeOwnershipLock(Path(path)/'state.sqlite/prediction_arbitrage/runtime.lock')
    lock.acquire()
    kind, phase = scenario.split('-')
    try:
        public = RefillPublic(build.clock)
        public.prices[5] = '.89'
        store, _, account, lp, _, _ = prepare(build, public=public)
        account.orders = _four_buys()[:3] if kind == 'augment' else _four_buys()
        market, condition, token = _refill_identity(5)
        request = dict(market_id=market, condition_id=condition, token_id=token,
            outcome='YES', price='.89', quantity='20', review_at=build.clock[0]+timedelta(hours=4))
        if kind == 'augment':
            base = lp.submit_entry(request, idempotency_key='existing-manual-buy')
            session = store.lp_session_by_idempotency('existing-manual-buy')
            assert session and session['entry_order_id'], base
            sid = session['session_id']
            account.posts.clear()

        def checkpoint():
            session = next(s for s in store.lp_sessions() if s['token_id'] == token)
            actions = store.lp_actions(session['session_id'])
            assert any(a['state'] == 'pending' and a['role'] == ('entry' if kind == 'manual' else 'augment') for a in actions)
            ready.send((session['session_id'], build.clock[0], account.orders, actions))
            assert release.wait(10), 'Independent old BUY owner watchdog'

        if phase == 'signing':
            sign = account.create_limit_order
            def blocked_sign(**kwargs):
                checkpoint()
                return sign(**kwargs)
            account.create_limit_order = blocked_sign
        else:
            post = account.post_order
            def blocked_post(signed):
                checkpoint()
                return post(signed)
            account.post_order = blocked_post
        if kind == 'manual':
            lp.submit_entry(request, idempotency_key='interrupted-manual-buy')
        else:
            result = lp.submit_augment(sid, '20', idempotency_key='interrupted-augment-buy', price='.88')
            assert result.get('state') != 'rejected', result
    finally:
        lock.release()
        fixture.close()
        patch.undo()


@pytest.fixture
def interrupted_buy_owner(tmp_path, runtime, request):
    context = multiprocessing.get_context('spawn')
    read, write = context.Pipe(duplex=False)
    release = context.Event()
    child = context.Process(target=_old_buy_owner, args=(str(tmp_path), request.param, write, release))
    child.start()
    write.close()
    try:
        assert read.poll(10), 'Independent durable BUY registration watchdog'
        sid, boundary, orders, actions = read.recv()
        runtime.clock[0] = boundary + timedelta(seconds=1)
        yield child, sid, orders, actions
    finally:
        if child.is_alive():
            child.terminate()
        child.join(5)
        if child.is_alive():
            child.kill()
            child.join(5)
        read.close()
        assert not child.is_alive()


@pytest.mark.parametrize('interrupted_buy_owner', ['manual-signing', 'manual-post', 'augment-signing', 'augment-post'], indirect=True)
def test_dead_manual_and_augment_buy_are_covered_once_and_refill_from_current_api(
        runtime, interrupted_buy_owner, monkeypatch, tmp_path):
    child, sid, orders, audit = interrupted_buy_owner
    _terminate_old(child)
    owner, store, _, account, _ = _new_owner(runtime, monkeypatch, tmp_path, orders=orders)
    owner.start()
    try:
        _advance(runtime)
        assert owner.execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        state = owner.execution.lp_auto_state()
        facts = owner.execution._auto_pool._read()['account_financial_facts']
        assert facts['financial_status'] == 'known', facts
        assert 'account_send_inflight' not in facts['reason_codes']
        assert state['slots']['occupied'] == 4
        assert state['funds']['status'] == 'known'
        assert store.lp_actions(sid) == audit
        # Augment has a real resting fifth-market order; the missing slot is
        # market4. Manual entry was interrupted without an account order.
        index = 4 if any(a['role'] == 'augment' for a in audit) else 5
        _arm_refill(runtime, owner, account, index=index)
        state = owner.execution.lp_auto_run_once(round_id='owner-ended-refill')
        assert state['slots']['occupied'] == len(account.orders) == 5, state['last_round']
        assert len(account.posts) == 1
        assert store.lp_actions(sid) == audit
        owner.execution.lp_auto_run_once(round_id='owner-ended-refill')
        assert len(account.posts) == 1
        current_orders = account.orders
    finally:
        owner.stop()
    owner, store, _, account, _ = _new_owner(runtime, monkeypatch, tmp_path, orders=current_orders)
    owner.start()
    try:
        _advance(runtime)
        owner.execution.refresh_lp_dashboard_snapshot()
        state = owner.execution.lp_auto_run_once(round_id='after-owner-ended-restart')
        assert state['slots']['occupied'] == 5
        assert state['funds']['status'] == 'known'
        assert store.lp_actions(sid) == audit
        assert account.posts == account.cancels == []
    finally:
        owner.stop()


@pytest.mark.parametrize('lifecycle', ['unknown', 'sending'])
@pytest.mark.parametrize('partial', [False, True], ids=['unfilled', 'partial-fill'])
def test_exact_api_buy_is_not_counted_twice_by_unknown_local_lifecycle(runtime, lifecycle, partial):
    store, adapter, account, lp, execution, _, originals = _historical_api_buy_intents(runtime)
    if partial:
        account.orders = (account.orders[0].model_copy(update={'size_matched': Decimal('5')}), *account.orders[1:])
        account.positions = (_position(size='5').model_copy(update={
            'token_id': originals[0]['token_id'], 'condition_id': originals[0]['condition_id']}),)
        _advance(runtime)
        lp.register_account_snapshot(adapter.lp_account_snapshot_shared(max_age_seconds=0,
            trade_generation_provider=store.lp_trade_generation))
    execution._auto_pool._update(lambda d: d['intents'][originals[0]['intent_id']].update(
        state=lifecycle, financial_status='unknown', submission_unknown=True,
        inventory_cost_usd='4' if partial else '0'))
    audit = deepcopy(execution.lp_auto_state()['intents'])
    actions = store.lp_actions(originals[0]['session_id'])
    state = execution.lp_auto_state()
    assert state['slots']['occupied'] == 3
    assert Decimal(state['funds']['buy_reserved_usd']) == (22 if partial else 24)
    assert Decimal(state['funds']['inventory_cost_usd']) == (2 if partial else 0)
    assert Decimal(state['funds']['spendable_usd']) == 16
    assert state['funds']['status'] == 'known'
    assert state['intents'] == audit
    assert store.lp_actions(originals[0]['session_id']) == actions
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('kind', ['manual', 'augment'])
def test_current_owner_completed_timeout_is_covered_by_later_api_without_restart(runtime, kind):
    public = RefillPublic(runtime.clock)
    public.prices[5] = '.89'
    store, adapter, account, lp, execution, _ = prepare(runtime, public=public)
    account.orders = _four_buys()[:3] if kind == 'augment' else _four_buys()
    market, condition, token = _refill_identity(5)
    request = dict(market_id=market, condition_id=condition, token_id=token,
        outcome='YES', price='.89', quantity='20', review_at=runtime.clock[0]+timedelta(hours=4))
    if kind == 'augment':
        lp.submit_entry(request, idempotency_key='current-owner-base')
        sid = store.lp_session_by_idempotency('current-owner-base')['session_id']
    def timeout(signed):
        raise TimeoutError('offline completed POST timeout')
    account.post_order = timeout
    if kind == 'manual':
        lp.submit_entry(request, idempotency_key='current-owner-timeout')
        sid = store.lp_session_by_idempotency('current-owner-timeout')['session_id']
    else:
        lp.submit_augment(sid, '20', idempotency_key='current-owner-timeout', price='.88')
    audit = deepcopy(store.lp_actions(sid))
    assert any(a['state'] == 'unknown' for a in audit)
    assert not store.lp_session(sid).get('submission_owner_exit')
    _advance(runtime)
    lp.register_account_snapshot(adapter.lp_account_snapshot_shared(max_age_seconds=0,
        trade_generation_provider=store.lp_trade_generation))
    state = execution.lp_auto_state()
    assert state['slots']['occupied'] == 4, state
    assert state['funds']['status'] == 'known'
    assert not state['admission_block_reasons']
    assert store.lp_actions(sid) == audit


@pytest.mark.parametrize('dead_preparation', ['awaiting-receipt'], indirect=True)
@pytest.mark.parametrize('exposure', ['buy', 'position'])
def test_dead_post_api_exposure_is_managed_once_without_replaying_unknown_request(
        runtime, dead_preparation, monkeypatch, tmp_path, exposure):
    child, original, _ = dead_preparation
    _terminate_old(child)
    owner, store, _, account, public = _new_owner(runtime, monkeypatch, tmp_path)
    public.prices[5] = '.89'
    sid = original['session_id']
    audit = deepcopy(store.lp_actions(sid))
    if exposure == 'buy':
        account.orders += (_open_order('late-post-buy', 'BUY', price='.89', original='20',
            token_id=original['token_id']).model_copy(update={
                'market': original['condition_id'], 'condition_id': original['condition_id']}),)
    else:
        account.positions = (_position(size='5').model_copy(update={
            'token_id': original['token_id'], 'condition_id': original['condition_id']}),)
    owner.start()
    try:
        _advance(runtime)
        owner.execution.refresh_lp_dashboard_snapshot()
        state = owner.execution.lp_auto_state()
        assert state['slots']['occupied'] == (5 if exposure == 'buy' else 4)
        assert state['funds']['status'] == 'known'
        session = store.lp_session(sid)
        assert session['reservation_coverage']
        assert session['state'] == 'entry_open', session
        assert not owner.lp._has_unresolved_submission(session)
        assert store.lp_actions(sid) == audit
        assert account.posts == account.cancels == []
    finally:
        owner.stop()


@pytest.mark.parametrize('interrupted_buy_owner', ['manual-signing', 'manual-post', 'augment-signing', 'augment-post'], indirect=True)
def test_live_manual_and_augment_sender_has_no_owner_exit_coverage(
        runtime, interrupted_buy_owner):
    child, sid, orders, audit = interrupted_buy_owner
    store, adapter, account, lp, execution = runtime(public_client=RefillPublic(runtime.clock), orders=orders)
    assert child.is_alive()
    _advance(runtime)
    lp.register_account_snapshot(adapter.lp_account_snapshot_shared(max_age_seconds=0,
        trade_generation_provider=store.lp_trade_generation))
    facts = execution._auto_pool._read()['account_financial_facts']
    assert facts['financial_status'] == 'unknown'
    assert 'account_send_inflight' in facts['reason_codes']
    assert not store.lp_session(sid).get('account_action_coverage')
    assert store.lp_actions(sid) == audit
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('interrupted_buy_owner', ['manual-post', 'augment-post'], indirect=True)
@pytest.mark.parametrize('invalid', ['pre-end', 'stale', 'incomplete', 'account', 'generation', 'session', 'action'])
def test_owner_ended_buy_still_requires_post_boundary_and_valid_unchanged_api(
        runtime, interrupted_buy_owner, monkeypatch, tmp_path, invalid):
    child, sid, orders, audit = interrupted_buy_owner
    # Establish the protected current account ledger while its sender is live.
    # Without an account publication the legacy empty-pool projection has no
    # account facts to label UNKNOWN; that is not proof of a released hold.
    _, live_adapter, _, live_lp, live_execution = runtime(public_client=RefillPublic(runtime.clock), orders=orders)
    _advance(runtime)
    live_lp.register_account_snapshot(live_adapter.lp_account_snapshot_shared(max_age_seconds=0,
        trade_generation_provider=live_lp.store.lp_trade_generation))
    assert live_execution.lp_auto_state()['funds']['status'] == 'unknown'
    _terminate_old(child)
    owner, store, adapter, account, _ = _new_owner(runtime, monkeypatch, tmp_path, orders=orders)
    owner.start()
    pending = next(a for a in audit if a['state'] == 'pending')
    try:
        certificate = store.lp_session(sid)['submission_owner_exit'][pending['action_id']]
        _advance(runtime)
        snapshot = adapter.lp_account_snapshot_shared(max_age_seconds=0, trade_generation_provider=store.lp_trade_generation)
        if invalid == 'pre-end':
            snapshot['read_started_at'] = certificate['ended_at']
        elif invalid == 'stale':
            _advance(runtime, 61)
        elif invalid == 'incomplete':
            snapshot['positions_complete'] = False
        elif invalid == 'account':
            snapshot['wallet_address'] = '0x'+'b'*40
        elif invalid == 'generation':
            store.lp_advance_trade_generation(store.lp_trade_generation())
        elif invalid == 'session':
            # A new durable POST boundary cannot inherit the old owner's end.
            store.lp_update_session(sid, patch={'submit_post_started_at': runtime.clock[0].isoformat()})
        else:
            store.lp_upsert_action(sid, pending['action_key'], state='pending',
                payload={**pending, 'submit_requested_at': runtime.clock[0].isoformat()})
        owner.lp.register_account_snapshot(snapshot)
        session = store.lp_session(sid)
        assert not (session.get('account_action_coverage') or {}).get(pending['action_id'])
        assert owner.execution.lp_auto_state()['funds']['status'] == 'unknown'
        assert account.posts == account.cancels == []
    finally:
        owner.stop()


@pytest.mark.parametrize('interrupted_buy_owner', ['augment-post'], indirect=True)
def test_new_live_augment_after_coverage_keeps_its_own_risk_and_never_reuses_old_end(
        runtime, interrupted_buy_owner, monkeypatch, tmp_path):
    child, sid, orders, audit = interrupted_buy_owner
    _terminate_old(child)
    owner, store, adapter, account, public = _new_owner(runtime, monkeypatch, tmp_path, orders=orders)
    public.prices[5] = '.89'
    owner.start()
    entered, release = Event(), Event()
    old = next(a for a in audit if a['role'] == 'augment')
    try:
        _advance(runtime)
        owner.execution.refresh_lp_dashboard_snapshot()
        marker = deepcopy(store.lp_session(sid)['account_action_coverage'][old['action_id']])
        def signing(**kwargs):
            entered.set()
            assert release.wait(5), 'Independent new BUY watchdog'
            _advance(runtime)
            raise TimeoutError('offline new preparation ended')
        account.create_limit_order = signing
        with ThreadPoolExecutor(1) as workers:
            future = workers.submit(owner.lp.submit_augment, sid, '20', 'new-owner-attempt', '.88')
            try:
                assert entered.wait(5)
                _advance(runtime)
                owner.lp.register_account_snapshot(adapter.lp_account_snapshot_shared(max_age_seconds=0,
                    trade_generation_provider=store.lp_trade_generation))
                state = owner.execution.lp_auto_state()
                assert state['slots']['occupied'] == 5
                assert state['funds']['status'] == 'unknown'
                assert state['admission_block_reasons']
                session = store.lp_session(sid)
                new = next(a for a in store.lp_actions(sid) if a['action_key'].endswith('new-owner-attempt'))
                assert new['action_id'] not in session['submission_owner_exit']
                assert new['action_id'] not in session['account_action_coverage']
                assert session['account_action_coverage'][old['action_id']] == marker
                assert account.posts == []
            finally:
                release.set()
            future.result(timeout=5)
    finally:
        release.set()
        owner.stop()


@pytest.mark.parametrize('missing', ['price', 'quantity', 'condition'])
def test_legacy_unfinished_metadata_is_explicit_unknown_without_projection_exception(runtime, missing):
    store, adapter, account, lp, execution, _ = prepare(runtime)
    account.orders = _four_buys()
    session = store.lp_create_session('legacy-manual', 'legacy-manual-key', state='entry_submit_pending',
        payload=dict(account_id=adapter.config.wallet_address, token_id=_refill_identity(5)[2],
            condition_id=_refill_identity(5)[1], price='.4', quantity='20'))
    if missing == 'condition':
        store.lp_update_session(session['session_id'], patch={'condition_id': None})
    else:
        store.lp_update_session(session['session_id'], patch={missing: None})
    store.lp_upsert_action(session['session_id'], 'legacy-manual:entry-submit', state='pending',
        payload=dict(role='entry', side='BUY', token_id=_refill_identity(5)[2]))
    _advance(runtime)
    lp.register_account_snapshot(adapter.lp_account_snapshot_shared(max_age_seconds=0,
        trade_generation_provider=store.lp_trade_generation))
    state = execution.lp_auto_state()
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['spendable_usd'] is None
    assert state['admission_block_reasons']


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_same_id_unknown_is_deduplicated_but_true_independent_risk_stays_separate(runtime, side):
    store, adapter, account, lp, execution, _, originals = _historical_api_buy_intents(runtime)
    original = originals[0]
    execution._auto_pool._update(lambda d: d['intents'][original['intent_id']].update(
        state='unknown', submission_unknown=True))
    store.lp_upsert_action(original['session_id'], original['session_id']+':augment-submit:new-risk'
        if side == 'BUY' else original['session_id']+':protected-exit-new-risk', state='pending',
        payload=dict(role='augment' if side == 'BUY' else 'protected_exit', side=side,
            token_id=original['token_id'], price='.4', quantity='20', submit_requested_at=lp._now().isoformat()))
    audit = deepcopy(execution.lp_auto_state()['intents'])
    actions = deepcopy(store.lp_actions(original['session_id']))
    _advance(runtime)
    lp.register_account_snapshot(adapter.lp_account_snapshot_shared(max_age_seconds=0,
        trade_generation_provider=store.lp_trade_generation))
    state = execution.lp_auto_state()
    assert state['slots']['occupied'] == (4 if side == 'BUY' else 3)
    assert Decimal(state['funds']['buy_reserved_usd']) == (32 if side == 'BUY' else 24)
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['spendable_usd'] is None
    assert state['admission_block_reasons']
    assert state['intents'] == audit
    assert store.lp_actions(original['session_id']) == actions
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('kind', ['manual', 'augment'])
@pytest.mark.parametrize('phase', ['signing', 'post'])
def test_http_buy_sender_keeps_runtime_owner_until_drain_then_account_coverage(
        runtime, monkeypatch, tmp_path, kind, phase):
    from tests.test_prediction_service import _running_server, _production_request, _response
    from open_trader.prediction_runtime import PredictionRuntimeOwnershipError
    public = RefillPublic(runtime.clock)
    prepare(runtime, count=6, public=public)
    orders = _four_buys()[:3] if kind == 'augment' else _four_buys()
    owner, store, _, account, public = _new_owner(runtime, monkeypatch, tmp_path, orders=orders)
    owner.start()
    _arm_refill(runtime, owner, account, index=5)
    market, condition, token = _refill_identity(5)
    request = dict(market_id=market, condition_id=condition, token_id=token, outcome='YES',
        price='.40', quantity='20', review_at=(runtime.clock[0]+timedelta(hours=4)).isoformat(),
        idempotency_key='http-interrupted-buy')
    if kind == 'augment':
        base = owner.execution.lp_submit_entry({**request, 'idempotency_key': 'http-existing-entry'})
        assert base['state'] == 'entry_open', base
        sid = base['session_id']
        request = dict(session_id=sid, quantity='20', price='.39', idempotency_key='http-interrupted-buy')
        account.posts.clear()
    entered, release = Event(), Event()
    def interrupted(*args, **kwargs):
        entered.set()
        assert release.wait(10), 'Independent HTTP sender watchdog'
        _advance(runtime)
        raise TimeoutError('offline HTTP sender completed')
    if phase == 'signing':
        account.create_limit_order = interrupted
    else:
        account.post_order = interrupted
    old_lp = owner.lp
    try:
        with _running_server(owner, session_token='session-token', csrf_token='csrf-token') as (url, server):
            with ThreadPoolExecutor(1) as workers:
                import json
                future = workers.submit(_response, _production_request(url,
                    '/api/prediction-arbitrage/lp/orders' if kind == 'manual' else '/api/prediction-arbitrage/lp/augment',
                    data=json.dumps(request).encode()), timeout=12)
                try:
                    assert entered.wait(5), 'HTTP route must reach real signing/POST'
                    sid = next(s['session_id'] for s in store.lp_sessions() if s['token_id'] == token)
                    audit = deepcopy(store.lp_actions(sid))
                    assert any(a['state'] == 'pending' for a in audit)
                    assert server.daemon_threads, 'Exercise the real HTTP shutdown gap'
                    with pytest.raises(RuntimeError, match='sender'):
                        owner.stop()
                    assert owner.state == 'STOPPING' and owner.production_owner
                    assert owner.store is not None and owner._prediction_trading is not None
                    assert not old_lp._mutation_allowed('submit')
                    challenger, _, _, _, _ = _new_owner(runtime, monkeypatch, tmp_path, orders=account.orders)
                    with pytest.raises(PredictionRuntimeOwnershipError):
                        challenger.start()
                    assert not store.lp_session(sid).get('submission_owner_exit')
                    assert store.lp_actions(sid) == audit
                    assert not future.done() and account.posts == []
                finally:
                    release.set()
                status, result = future.result(timeout=5)
                local_prepost_failure = kind == 'manual' and phase == 'signing'
                assert status == 200 and result['state'] == ('entry_rejected' if local_prepost_failure else 'needs_attention'), result
                finished = deepcopy(store.lp_actions(sid))
                assert any(a['state'] == ('rejected' if local_prepost_failure else 'unknown') for a in finished)
                current_orders = account.orders
                owner.stop()
                assert owner.state == 'STOPPED' and not owner.production_owner
                assert account.posts == []
        new, store, _, account, _ = _new_owner(runtime, monkeypatch, tmp_path, orders=current_orders)
        new.start()
        try:
            _advance(runtime)
            refreshed = new.execution.refresh_lp_dashboard_snapshot()
            assert refreshed['state'] == 'ready', refreshed
            state = new.execution.lp_auto_state()
            assert state['funds']['status'] == 'known'
            assert state['slots']['occupied'] == 4, state
            assert store.lp_actions(sid) == finished
            if not local_prepost_failure:
                assert store.lp_session(sid)['account_action_coverage']
            assert account.posts == account.cancels == []
        finally:
            new.stop()
    finally:
        release.set()
        owner.stop()


@pytest.mark.parametrize('boundary', ['stopping', 'handed-off'])
def test_http_request_with_execution_reference_cannot_begin_buy_after_shutdown(
        runtime, monkeypatch, tmp_path, boundary):
    from tests.test_prediction_service import _running_server, _production_request, _response
    import json
    public = RefillPublic(runtime.clock)
    prepare(runtime, count=6, public=public)
    owner, store, _, account, _ = _new_owner(runtime, monkeypatch, tmp_path)
    owner.start()
    _arm_refill(runtime, owner, account, index=5)
    def body(index, key):
        market, condition, token = _refill_identity(index)
        return dict(market_id=market, condition_id=condition, token_id=token, outcome='YES',
            price='.40', quantity='20', review_at=(runtime.clock[0]+timedelta(hours=4)).isoformat(),
            idempotency_key=key)
    entered, release = Event(), Event()
    queued, resume = Event(), Event()
    def signing(**kwargs):
        entered.set()
        assert release.wait(10), 'Independent sender watchdog'
        _advance(runtime)
        raise TimeoutError('offline original sender ended')
    account.create_limit_order = signing
    execution = owner.execution
    submit = execution.lp_submit_entry
    def delayed(payload):
        if payload['idempotency_key'] == 'queued-http-entry':
            queued.set()
            assert resume.wait(10), 'Independent captured-execution watchdog'
        return submit(payload)
    execution.lp_submit_entry = delayed
    new = None
    try:
        with _running_server(owner, session_token='session-token', csrf_token='csrf-token') as (url, _):
            with ThreadPoolExecutor(2) as workers:
                first = workers.submit(_response, _production_request(url,
                    '/api/prediction-arbitrage/lp/orders', data=json.dumps(body(5, 'active-http-entry')).encode()), timeout=12)
                late = None
                try:
                    assert entered.wait(5)
                    late = workers.submit(_response, _production_request(url,
                        '/api/prediction-arbitrage/lp/orders', data=json.dumps(body(6, 'queued-http-entry')).encode()), timeout=12)
                    assert queued.wait(5), 'Handler obtained execution before STOPPING'
                    with pytest.raises(RuntimeError, match='sender'):
                        owner.stop()
                    release.set()
                    assert first.result(timeout=5)[0] == 200
                    if boundary == 'handed-off':
                        owner.stop()
                        new, _, _, _, _ = _new_owner(runtime, monkeypatch, tmp_path)
                        new.start()
                        assert new.production_owner and not owner.production_owner
                    else:
                        assert owner.state == 'STOPPING' and owner.production_owner
                    resume.set()
                    status, result = late.result(timeout=5)
                    assert status == 200 and result['state'] == 'locked', result
                    assert result['reason'] == 'mutation_blocked'
                    assert not store.lp_session_by_idempotency('queued-http-entry')
                    assert account.posts == []
                finally:
                    release.set()
                    resume.set()
                    first.result(timeout=5)
                    if late is not None:
                        late.result(timeout=5)
    finally:
        release.set()
        resume.set()
        if new is not None:
            new.stop()
        owner.stop()


@pytest.mark.parametrize('action_state', ['accepted_without_order_id', 'accepted'])
@pytest.mark.parametrize('dead_preparation', ['awaiting-receipt'], indirect=True)
def test_direct_manual_submit_reserves_uncovered_idless_independent_buy(
        runtime, dead_preparation, monkeypatch, tmp_path, action_state):
    _check_idless_buy_guard(runtime, dead_preparation, monkeypatch, tmp_path, action_state)


@pytest.mark.parametrize('action_state', ['pending', 'accepted_without_order_id', 'accepted'])
@pytest.mark.parametrize('dead_preparation', ['awaiting-receipt'], indirect=True)
@pytest.mark.parametrize('checkpoint', ['publication', 'manual'])
@pytest.mark.parametrize('suffix', ['cancelable', 'entry-submit'])
def test_user_augment_key_does_not_hide_unresolved_buy(
        runtime, dead_preparation, monkeypatch, tmp_path, action_state, checkpoint, suffix):
    _check_idless_buy_guard(runtime, dead_preparation, monkeypatch, tmp_path, action_state,
        suffix=suffix, check_publication=checkpoint == 'publication')


def _check_idless_buy_guard(runtime, dead_preparation, monkeypatch, tmp_path, action_state,
        *, suffix='unresolved', check_publication=True):
    child, original, _ = dead_preparation
    _terminate_old(child)
    target = _open_order('actual-old-buy', 'BUY', price='.89', original='20',
        token_id=original['token_id']).model_copy(update={
            'market': original['condition_id'], 'condition_id': original['condition_id']})
    owner, store, adapter, account, public = _new_owner(runtime, monkeypatch, tmp_path,
        orders=_four_buys()[:3]+(target,))
    public.prices[5] = '.89'
    owner.start()
    try:
        _advance(runtime)
        owner.execution.refresh_lp_dashboard_snapshot()
        sid = original['session_id']
        assert store.lp_session(sid)['reservation_coverage']
        assert store.lp_session(sid)['state'] == 'entry_open'
        action = store.lp_upsert_action(sid, sid+':augment-submit:'+suffix, state=action_state,
            payload=dict(role='augment', side='BUY', token_id=original['token_id'],
                price='.40', quantity='20', submit_stage='sending', submit_requested_at=runtime.clock[0].isoformat()))
        _advance(runtime)
        owner.lp.register_account_snapshot(adapter.lp_account_snapshot_shared(max_age_seconds=0,
            trade_generation_provider=store.lp_trade_generation))
        facts = owner.execution._auto_pool._read()['account_financial_facts']
        if check_publication:
            from open_trader.polymarket_lp_accounting import has_independent_unresolved_action
            assert facts['financial_status'] == 'unknown' and 'account_send_inflight' in facts['reason_codes']
            assert any(r['intent_id'] == 'action:'+action['action_id'] for r in facts['pending_buy_actions'])
            assert has_independent_unresolved_action(store.lp_session(sid), [action])
        audit = deepcopy(store.lp_actions(sid))
        market, condition, token = _refill_identity(6)
        store.lp_save_price_history(condition, token, [], dict(state='known', amplitude=Decimal('.005'),
            checked_at=owner.lp._now(), valid_until=owner.lp._now()+timedelta(days=1)))
        _arm_refill(runtime, owner, account, index=6)
        result = owner.execution.lp_submit_entry(dict(market_id=market, condition_id=condition,
            token_id=token, outcome='YES', price='.40', quantity='20',
            review_at=(runtime.clock[0]+timedelta(hours=4)).isoformat(), idempotency_key='manual-new-token'))
        assert result['state'] == 'rejected', result
        assert result['reason'] == 'account_facts_unknown'
        assert account.posts == []
        assert not store.lp_session_by_idempotency('manual-new-token')
        assert store.lp_actions(sid) == audit
    finally:
        owner.stop()


@pytest.mark.parametrize('operation, role', [
    ('augment-cancel', 'augment-cancel'), ('owned-cancel', 'entry'),
    ('reconciliation-cancel', 'reconciliation_cancel'),
    ('entry-protection-cancel', 'entry-protection-cancel'),
    ('first-seen-protection-cancel', 'first-seen-protection-cancel'),
    ('augment-cancel', None),
])
def test_real_cancel_operation_never_reserves_a_new_buy(runtime, operation, role):
    store, adapter, account, lp, execution, _, originals = _historical_api_buy_intents(runtime)
    original = originals[0]
    sid = original['session_id']
    facts = execution._auto_pool._read()['account_financial_facts']
    store.lp_update_session(sid, patch=dict(reservation_coverage=dict(version=1, state='covered',
        account_id=facts['account_id'], pool_account_id=facts['pool_account_id'],
        snapshot_id=facts['snapshot_id'], session_id=sid, read_started_at=facts['read_started_at'])))
    action = store.lp_upsert_action(sid, sid+':'+operation+':'+original['order_id'], state='pending',
        payload=dict(role=role, side='BUY', token_id=original['token_id'],
            order_id=original['order_id'], targets=[original['order_id']], price='.40', quantity='20'))
    audit = deepcopy(store.lp_actions(sid))
    _advance(runtime)
    lp.register_account_snapshot(adapter.lp_account_snapshot_shared(max_age_seconds=0,
        trade_generation_provider=store.lp_trade_generation))
    facts = execution._auto_pool._read()['account_financial_facts']
    assert facts['financial_status'] == 'known'
    assert facts['pending_buy_actions'] == []
    buy = next(b for b in facts['buys'] if b['order_id'] == original['order_id'])
    assert buy['state'] == 'canceling'
    assert execution.lp_auto_state()['slots']['occupied'] == 3
    assert not any(r['order_id'] == 'lp-action:'+action['action_id'] for r in lp._candidate_reservations())
    assert store.lp_actions(sid) == audit
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('side, role', [('BUY', 'augment'), ('SELL', 'protected_exit'), ('BUY', None), ('SELL', None)])
@pytest.mark.parametrize('state', ['pending', 'accepted_without_order_id', 'accepted'])
@pytest.mark.parametrize('suffix', ['cancelable', 'entry-submit'])
def test_user_key_suffix_does_not_hide_independent_buy_or_sell(side, role, state, suffix):
    from open_trader.polymarket_lp_accounting import has_independent_unresolved_action
    operation = 'augment-submit' if side == 'BUY' else 'protected-submit'
    action = dict(session_id='session', action_key='session:'+operation+':'+suffix,
        role=role, side=side, state=state)
    assert has_independent_unresolved_action(dict(session_id='session'), [action])


def test_augment_submit_named_cancelable_does_not_tag_api_buy_canceling(runtime):
    store, adapter, account, lp, execution, _, originals = _historical_api_buy_intents(runtime)
    original = originals[0]
    sid = original['session_id']
    store.lp_upsert_action(sid, sid+':augment-submit:cancelable', state='accepted',
        payload=dict(role='augment', side='BUY', token_id=original['token_id'], order_id=original['order_id']))
    audit = deepcopy(store.lp_actions(sid))
    _advance(runtime)
    lp.register_account_snapshot(adapter.lp_account_snapshot_shared(max_age_seconds=0,
        trade_generation_provider=store.lp_trade_generation))
    facts = execution._auto_pool._read()['account_financial_facts']
    assert facts['financial_status'] == 'known'
    assert next(b for b in facts['buys'] if b['order_id'] == original['order_id'])['state'] == 'active'
    assert execution.lp_auto_state()['slots']['occupied'] == 3
    assert store.lp_actions(sid) == audit
    assert account.posts == account.cancels == []


def test_cancel_receipt_with_entry_role_cannot_prove_original_entry_ended(runtime):
    from open_trader.polymarket_lp_accounting import ended_reservation_evidence
    store, adapter, _, lp, execution, _ = prepare(runtime)
    market, condition, token = _refill_identity(5)
    intent = dict(intent_id='legacy-entry', session_id='legacy', state='unknown',
        market_id=market, condition_id=condition, token_id=token, outcome='YES')
    session = store.lp_create_session('legacy', 'lp-auto:legacy-entry', state='needs_attention',
        payload=dict(account_id=adapter.config.wallet_address, wallet_address=adapter.config.wallet_address,
            market_id=market, condition_id=condition, token_id=token, outcome='YES', submit_status='unknown'))
    store.lp_upsert_action('legacy', 'legacy:owned-cancel:old-order', state='accepted',
        payload=dict(role='entry', side='BUY', order_id='old-order', token_id=token))
    _advance(runtime)
    assert ended_reservation_evidence(intent, session, store.lp_actions('legacy'),
        account_id=adapter.config.wallet_address, pool_account_id=execution._auto_pool._read()['account_id'],
        read_started_at=lp._now()) is None


@pytest.mark.parametrize('checkpoint', ['release', 'retirement'])
@pytest.mark.parametrize('state', ['accepted_without_order_id', 'accepted'])
def test_manual_waiver_cannot_retire_independent_idless_buy(runtime, checkpoint, state):
    from tests.test_lp_account_reservation_reconciliation import _seed_unknowns
    from tests.test_lp_manual_reservation_release import _release
    store, _, account, _, execution = runtime()
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=5))
    original = _seed_unknowns(store, execution, count=1, ownership=False)[0]
    sid = original['session_id']
    if checkpoint == 'retirement':
        assert _release(execution, intent_id=original['intent_id'])['released']
    action = store.lp_upsert_action(sid, sid+':augment-submit:entry-submit', state=state,
        payload=dict(role='augment', side='BUY', token_id=original['token_id'], submit_stage='sending'))
    if checkpoint == 'release':
        result = _release(execution, intent_id=original['intent_id'])
        assert not result['released'], result
        assert result['skipped'][0]['reason'] == 'independent_action_unresolved'
        assert not store.lp_session(sid).get('manual_reservation_release')
    else:
        store.lp_update_session(sid, state='needs_attention', patch={'facts_error': 'new_send_unknown'})
        assert store.lp_session(sid)['state'] == 'needs_attention'
    assert next(a for a in store.lp_actions(sid) if a['action_id'] == action['action_id']) == action
    assert account.posts == account.cancels == []


def test_manual_waiver_entry_audit_excludes_augment_user_suffix(runtime):
    from tests.test_lp_account_reservation_reconciliation import _seed_unknowns
    from tests.test_lp_manual_reservation_release import _release
    store, _, account, _, execution = runtime()
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=5))
    original = _seed_unknowns(store, execution, count=1, ownership=False)[0]
    sid = original['session_id']
    entry = store.lp_actions(sid)[0]
    store.lp_upsert_action(sid, sid+':augment-submit:entry-submit', state='accepted',
        payload=dict(role='augment', side='BUY', order_id='known-augment'))
    assert _release(execution, intent_id=original['intent_id'])['released']
    assert store.lp_session(sid)['manual_reservation_release']['original_entry_actions'] == [entry]
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('role', ['passive_exit_cancel', 'owned_sell_cancel'])
def test_real_passive_cancel_roles_are_pending_cancels(runtime, role):
    from open_trader.polymarket_lp_accounting import account_cancel_is_pending
    store, _, account, lp, execution, _, originals = _historical_api_buy_intents(runtime)
    original = originals[0]
    sid = original['session_id']
    # Exact _cancel_passive_exit_before_protected producer payload: no side field.
    action = store.lp_upsert_action(sid, sid+':passive-cancel:'+original['order_id'], state='accepted',
        payload=dict(role=role, order_id=original['order_id']))
    assert account_cancel_is_pending(store.lp_session(sid), [action], original['order_id'])
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('state', ['pending', 'accepted'])
def test_augment_user_cancel_suffix_retains_submission_audit_without_cancel_event(runtime, state):
    store, _, account, lp, execution, _, originals = _historical_api_buy_intents(runtime)
    original = originals[0]
    sid, oid = original['session_id'], 'actual-augment-order'
    session = store.lp_session(sid)
    owned = {**session['order_history'][original['order_id']], 'order_id': oid}
    store.lp_register_exchange_orders(session['account_id'], original['token_id'], [owned], session=session)
    action = store.lp_upsert_action(sid, sid+':augment-submit:cancelable', state=state,
        payload=dict(role='augment', side='BUY', token_id=original['token_id'], order_id=oid,
            price='.40', quantity='20', submit_receipt_at=lp._now().isoformat()))
    store.lp_update_session(sid, patch=dict(submit_status='accepted', position_reconciled=True, facts_error=None,
        buy_cost='0', buy_filled_quantity='0', sold_quantity='0', sold_revenue='0'))
    execution._auto_pool._record_session(original['intent_id'], store.lp_session(sid))
    document = execution._auto_pool._read()
    assert document['intents'][original['intent_id']]['submission_unknown'] is False
    events = list(document['events'].values())
    assert any(e['kind'] == 'intent' and e['intent_id'] == 'action:'+action['action_key'] for e in events)
    assert not any(e['kind'] == 'cancel_requested' and e['order_id'] == oid for e in events)
    if state == 'accepted':
        event = next(e for e in events if e['event_id'] == 'accepted:'+oid)
        assert event['intent_id'] == 'action:'+action['action_key']
        assert event['occurred_at'] == action['submit_receipt_at']
    store.lp_upsert_action(sid, sid+':entry-cancel:'+oid, state='pending',
        payload=dict(role='entry', order_id=oid, targets=[oid]))
    execution._auto_pool._record_session(original['intent_id'], store.lp_session(sid))
    document = execution._auto_pool._read()
    assert document['intents'][original['intent_id']]['submission_unknown'] is True
    assert document['events']['cancel_requested:'+oid]['kind'] == 'cancel_requested'
    assert account.posts == account.cancels == []


def test_recent_pending_cancel_ids_exclude_user_named_augment(runtime):
    store, _, account, lp, _, _, originals = _historical_api_buy_intents(runtime)
    sid = originals[0]['session_id']
    store.lp_upsert_action(sid, sid+':augment-submit:cancelable', state='pending',
        payload=dict(role='augment', side='BUY', order_id='live-augment'))
    store.lp_upsert_action(sid, sid+':owned-cancel:real', state='pending',
        payload=dict(role='entry', order_id='real-cancel'))
    assert lp._recent_pending_cancel_order_ids(sid) == {'real-cancel'}
    assert account.posts == account.cancels == []


def test_rotation_cancel_ids_keep_user_named_augment_as_protected_survivor(tmp_path, monkeypatch):
    from tests.test_lp_auto_account_projection import rotation_pool
    engine, account, lp, store, _ = rotation_pool(tmp_path, monkeypatch, same_token=True)
    posts = deepcopy(account.posts)
    anchor = next(b for b in engine.lp_auto_state()['account_buys'] if b['order_id'] == 'o1')
    sid = anchor['session_id']
    before = deepcopy(lp._queue_protection_levels(store.lp_session(sid)))
    store.lp_upsert_action(sid, sid+':augment-submit:cancelable', state='accepted',
        payload=dict(role='augment', side='BUY', order_id='manual-second'))
    engine._auto_pool._mark_rotation_cancel_requested(anchor)
    after = lp._queue_protection_levels(store.lp_session(sid))
    key = next(k for k, b in before.items() if b['order_id'] == 'o1')
    assert after[key] == {**before[key], 'order_id': 'manual-second'}
    assert account.posts == posts and account.cancels == []


def test_reconcile_terminal_order_does_not_accept_user_named_augment_as_cancel(runtime):
    store, _, account, lp, _, _, originals = _historical_api_buy_intents(runtime)
    original = originals[0]
    sid, oid = original['session_id'], original['order_id']
    action = store.lp_upsert_action(sid, sid+':augment-submit:cancelable', state='unknown',
        payload=dict(role='augment', side='BUY', token_id=original['token_id'], order_id=oid))
    cancel = store.lp_upsert_action(sid, sid+':owned-cancel:real', state='unknown',
        payload=dict(role='entry', order_id=oid, targets=[oid]))
    account.orders = tuple(o.model_copy(update={'status': 'CANCELED'}) if o.id == oid else o for o in account.orders)
    _advance(runtime)
    result = lp.reconcile_facts(sid)
    assert result[3] is None, result[3]
    actions = {a['action_id']: a for a in store.lp_actions(sid)}
    assert actions[action['action_id']] == action
    assert actions[cancel['action_id']]['state'] == 'accepted'
    assert account.posts == account.cancels == []
