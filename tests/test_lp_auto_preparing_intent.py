"""Preparing BUY reservations survive concurrent facts publication (#270)."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event

import pytest

from tests.test_lp_account_reservation_reconciliation import runtime, _advance, _refill_identity
from tests.test_lp_auto_refill_contract import prepare
from tests.test_lp_order_registration_contract import _open_order


@pytest.mark.parametrize('reconcile', [False, True], ids=['control', 'overlap'])
@pytest.mark.parametrize('phase', ['signing', 'presend-account-read'])
def test_three_to_five_keeps_both_preparing_reservations(runtime, reconcile, phase):
    store, adapter, account, lp, execution, _ = prepare(runtime)
    account.orders = tuple(_open_order(f'manual-{i}', 'BUY', price='.40', original='20',
        token_id=_refill_identity(i)[2]).model_copy(update={
            'market': _refill_identity(i)[1], 'condition_id': _refill_identity(i)[1]})
        for i in range(1, 4))
    entered, release = [Event(), Event()], [Event(), Event()]
    signing_entered, signing_release = [Event(), Event()], [Event(), Event()]
    original_sign = account.create_limit_order
    armed = [None]
    rounds = []

    def pause(index):
        entered[index].set()
        assert release[index].wait(5), 'Independent preparation watchdog'

    def sign(**kwargs):
        index = len(account.posts)
        if phase == 'signing':
            pause(index)
        else:
            signing_entered[index].set()
            assert signing_release[index].wait(5), 'Independent signing watchdog'
            armed[0] = index
        return original_sign(**kwargs)

    def positions_read(_):
        index, armed[0] = armed[0], None
        if index is not None:
            pause(index)

    account.create_limit_order = sign
    account.before_positions = positions_read
    with ThreadPoolExecutor(1) as workers:
        pending = workers.submit(execution.lp_auto_run_once, round_id='refill-two')
        try:
            for index in range(2):
                account_round = None
                if phase == 'presend-account-read':
                    assert signing_entered[index].wait(5)
                    # The adapter serializes private reads. Complete the
                    # reconciler's real round before blocking the send read;
                    # its facts publish while that later read is still active.
                    account_round = adapter.lp_account_round_begin(store.lp_trade_generation)
                    rounds.append(account_round)
                    adapter.lp_account_snapshot(account_round=account_round)
                    signing_release[index].set()
                assert entered[index].wait(5), 'Independent auto-submit watchdog'
                before = execution.lp_auto_state()
                intent = next(i for i in before['intents'] if i['state'] == 'reserved')
                sid = intent['session_id']
                assert lp.entry_send_inflight(sid)
                assert len(account.orders) == 3 + index and len(account.posts) == index
                action = next(a for a in store.lp_actions(sid) if a['role'] == 'entry')
                assert action['state'] == 'pending'
                assert action['submit_stage'] == 'preparing' and action['post_started'] is False
                # These fields are initially on the action, not the session.
                assert store.lp_session(sid).get('post_started') is None
                if reconcile:
                    lp.reconcile_facts(sid, apply_lock=(
                        execution._acquire_global_lock, execution._release_global_lock),
                        account_round=account_round)
                    after = execution.lp_auto_state()
                    current = next(i for i in after['intents'] if i['intent_id'] == intent['intent_id'])
                    assert current['state'] == 'reserved', current
                    assert current['reserved_usd'] == intent['reserved_usd']
                    assert current['financial_status'] == 'known'
                    assert after['slots']['occupied'] == before['slots']['occupied']
                    assert not current.get('reconcile_reason')
                release[index].set()
        finally:
            for event in (*release, *signing_release):
                event.set()
            for account_round in rounds:
                adapter.lp_account_round_end(account_round)
        state = pending.result(timeout=5)
    assert len(account.orders) == state['slots']['occupied'] == 5
    assert len(account.posts) == 2
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert state['funds']['status'] == 'known'
    _advance(runtime)
    assert execution.lp_auto_run_once(round_id='refill-two')['slots']['occupied'] == 5
    assert execution.lp_auto_run_once(round_id='next-round')['slots']['occupied'] == 5
    assert len(account.posts) == 2
    assert account.cancels == account.market_orders == []


@pytest.mark.parametrize('fence, reason', [
    ('pause', 'manually_paused'), ('stop', 'session_stopped_before_send'),
    ('config', 'config_version_changed'),
])
def test_preparing_publication_keeps_send_fences(runtime, fence, reason):
    store, _, account, lp, execution, _ = prepare(runtime, target=1, count=1)
    entered, release = Event(), Event()
    original_sign = account.create_limit_order

    def sign(**kwargs):
        entered.set()
        assert release.wait(5), 'Independent signing watchdog'
        return original_sign(**kwargs)

    account.create_limit_order = sign
    with ThreadPoolExecutor(1) as workers:
        pending = workers.submit(execution.lp_auto_run_once, round_id='fenced-entry')
        try:
            assert entered.wait(5)
            intent = execution.lp_auto_state()['intents'][0]
            sid = intent['session_id']
            lp.reconcile_facts(sid, apply_lock=(
                execution._acquire_global_lock, execution._release_global_lock))
            assert execution.lp_auto_state()['intents'][0]['state'] == 'reserved'
            if fence == 'pause':
                execution.lp_auto_set_desired_running(False)
            elif fence == 'stop':
                execution.lp_stop(sid)
            else:
                # Inject a durable version invalidation. Public configuration
                # correctly refuses edits while an entry is still occupied.
                execution._auto_pool._update(lambda d: d.update(config_version=d['config_version'] + 1, trading_config_version=d['trading_config_version'] + 1))
        finally:
            release.set()
        state = pending.result(timeout=5)
    assert account.posts == account.cancels == []
    assert state['last_round']['actions'][0]['reason'] == reason
    assert store.lp_session(sid)['post_started'] is False
    assert store.lp_actions(sid)[0]['state'] == 'rejected'


@pytest.mark.parametrize('uncertainty', [
    'no-lane', 'identity-conflict', 'independent-action', 'read-error',
    'action-unknown', 'action-token-mismatch', 'action-key-mismatch',
])
def test_preparing_uncertainty_still_becomes_unknown(runtime, uncertainty):
    store, _, account, lp, execution, public = prepare(runtime, target=1, count=1)
    entered, release = Event(), Event()
    original_sign = account.create_limit_order

    def sign(**kwargs):
        entered.set()
        assert release.wait(5), 'Independent signing watchdog'
        return original_sign(**kwargs)

    account.create_limit_order = sign
    with ThreadPoolExecutor(1) as workers:
        pending = workers.submit(execution.lp_auto_run_once, round_id='uncertain-entry')
        try:
            assert entered.wait(5)
            intent = execution.lp_auto_state()['intents'][0]
            sid = intent['session_id']
            reader, owner = lp, execution
            if uncertainty == 'no-lane':
                # A new service sees the same SQLite rows, but owns no live lane.
                _, _, _, reader, owner = runtime(public_client=public)
                assert not reader.entry_send_inflight(sid)
            elif uncertainty == 'identity-conflict':
                store.lp_update_session(sid, patch={'order_identity_conflict': {'reason': 'duplicate_identity'}})
            elif uncertainty == 'independent-action':
                store.lp_upsert_action(sid, f'{sid}:protected-exit', state='unknown',
                    payload={'role': 'protected_exit', 'side': 'SELL', 'token_id': intent['token_id']})
            elif uncertainty == 'read-error':
                account.list_positions = lambda **kwargs: (_ for _ in ()).throw(TimeoutError('offline account read'))
            else:
                action = store.lp_actions(sid)[0]
                if uncertainty == 'action-key-mismatch':
                    # Keep the apparent preparing action but remove the exact
                    # entry identity in this deliberately invalid durable fixture.
                    with store._transaction() as connection:
                        connection.execute('UPDATE lp_actions SET action_key=? WHERE action_id=?',
                            (f'{sid}:wrong-entry', action['action_id']))
                else:
                    store.lp_upsert_action(sid, action['action_key'],
                        state='unknown' if uncertainty == 'action-unknown' else 'pending',
                        payload={**action, 'token_id': _refill_identity(2)[2]
                            if uncertainty == 'action-token-mismatch' else action['token_id']})
            reader.reconcile_facts(sid, apply_lock=(owner._acquire_global_lock, owner._release_global_lock))
            if uncertainty == 'identity-conflict':
                # Reconcile detects a preexisting conflict before publishing.
                # Exercise the existing auto dispatch for that short circuit,
                # without replacing the live lane's owner or its publisher.
                owner._auto_pool._reconcile_intent(intent, reuse=False)
            current = owner.lp_auto_state()['intents'][0]
            assert current['financial_status'] == 'unknown'
            # Existing read errors mark financial UNKNOWN without replacing
            # the reserved lifecycle state; identity and missing IDs do replace it.
            assert current['state'] == ('reserved' if uncertainty == 'read-error' else 'unknown')
            assert current['reserved_usd'] == intent['reserved_usd']
            assert owner.lp_auto_state()['slots']['occupied'] == 1
            assert account.posts == []
        finally:
            release.set()
        pending.result(timeout=5)
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('receipt', ['late', 'missing', 'idless'])
@pytest.mark.parametrize('stale_reserved', [False, True], ids=['sending-intent', 'stale-reserved-intent'])
def test_post_started_with_stale_preparing_action_keeps_unknown_until_receipt(runtime, receipt, stale_reserved):
    store, _, account, lp, execution, _ = prepare(runtime, target=1, count=1)
    entered, release = Event(), Event()
    original_post = account.post_order

    def post(signed):
        entered.set()
        assert release.wait(5), 'Independent receipt watchdog'
        if receipt == 'late':
            return original_post(signed)
        account.posts.append(signed)
        if receipt == 'missing':
            raise TimeoutError('offline lost receipt')
        return {'accepted': True, 'status': 'LIVE'}

    account.post_order = post
    with ThreadPoolExecutor(1) as workers:
        pending = workers.submit(execution.lp_auto_run_once, round_id='late-entry')
        try:
            assert entered.wait(5)
            intent = execution.lp_auto_state()['intents'][0]
            sid = intent['session_id']
            assert lp.entry_send_inflight(sid)
            assert store.lp_session(sid)['post_started'] is True
            assert store.lp_session(sid)['submit_stage'] == 'sending'
            action = store.lp_actions(sid)[0]
            assert action['submit_stage'] == 'preparing' and action['post_started'] is False
            if stale_reserved:
                # A stale reserved ledger row must not outweigh the real
                # session's durable POST boundary or rely on the lagging action.
                execution._auto_pool._update(lambda d: d['intents'][intent['intent_id']].update(
                    state='reserved', financial_status='known'))
            lp.reconcile_facts(sid, apply_lock=(
                execution._acquire_global_lock, execution._release_global_lock))
            current = execution.lp_auto_state()['intents'][0]
            assert current['state'] == current['financial_status'] == 'unknown'
            assert current['reconcile_reason'] == 'missing_reliable_order_id'
            assert current['reserved_usd'] == intent['reserved_usd']
            assert not current.get('reservation_coverage')
            assert execution.lp_auto_state()['slots']['occupied'] == 1
        finally:
            release.set()
        state = pending.result(timeout=5)
    session, action = store.lp_session(sid), store.lp_actions(sid)[0]
    assert len(account.posts) == 1
    assert not lp.entry_send_inflight(sid)
    if receipt == 'late':
        assert session['entry_order_id'] == action['order_id'] == 'refill-1'
        assert action['state'] == 'accepted'
        assert state['slots']['occupied'] == 1
        assert state['funds']['status'] == 'known'
    else:
        assert not session['entry_order_id']
        assert session['submit_status'] == ('unknown' if receipt == 'missing' else 'accepted_without_order_id')
        assert action['state'] == ('unknown' if receipt == 'missing' else 'accepted')
        assert state['intents'][0]['state'] == 'unknown'
    execution.lp_auto_run_once(round_id='late-entry')
    assert len(account.posts) == 1
    assert account.cancels == account.market_orders == []
