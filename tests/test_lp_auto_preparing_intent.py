"""Preparing BUY reservations survive concurrent facts publication (#270)."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event

import pytest

from tests.test_lp_account_reservation_reconciliation import runtime, _advance, _refill_identity
from tests.test_lp_auto_refill_contract import prepare
from tests.test_lp_order_registration_contract import _open_order


@pytest.mark.parametrize('reconcile', [False, True], ids=['control', 'overlap'])
@pytest.mark.parametrize('phase', ['signing', 'after-sign'])
def test_three_to_five_keeps_both_preparing_reservations(runtime, reconcile, phase):
    import json
    from collections import Counter
    from copy import deepcopy
    from datetime import datetime
    store, adapter, account, lp, execution, public = prepare(runtime)
    original_orders = tuple(_open_order(f'manual-{i}', 'BUY', price='.40', original='20',
        token_id=_refill_identity(i)[2]).model_copy(update={
            'market': _refill_identity(i)[1], 'condition_id': _refill_identity(i)[1]}) for i in range(1, 4))
    account.orders = original_orders
    tokens = [_refill_identity(i)[2] for i in (4, 5)]
    entered = {token: Event() for token in tokens}
    release = {token: Event() for token in tokens}
    signing_entered = {token: Event() for token in tokens}
    signing_release = {token: Event() for token in tokens}
    original_sign = account.create_limit_order
    signed_objects, rounds, captures = {}, {}, []
    latest_financial_as_of, pending_audits = [], {}
    def sign(**kwargs):
        token = kwargs['token_id']
        assert token in entered
        if phase == 'signing':
            entered[token].set()
            assert release[token].wait(5), 'Independent preparation watchdog'
        else:
            signing_entered[token].set()
            assert signing_release[token].wait(5), 'Independent signing watchdog'
        signed = original_sign(**kwargs)
        signed_objects[token] = signed
        if phase == 'after-sign':
            entered[token].set()
            assert release[token].wait(5), 'Independent signed-result watchdog'
        return signed
    account.create_limit_order = sign
    with ThreadPoolExecutor(1) as workers:
        pending = workers.submit(execution.lp_auto_run_once, round_id='refill-two')
        try:
            if phase == 'after-sign':
                for token in tokens:
                    assert signing_entered[token].wait(5), 'Both exact SDK signers entered'
                for token in tokens:
                    account_round = adapter.lp_account_round_begin(store.lp_trade_generation)
                    rounds[token] = account_round
                    packet = adapter.lp_account_snapshot(account_round=account_round)
                    captures.append(dict(token=token, phase='pre-sign-account', packet=deepcopy(packet)))
                    signing_release[token].set()
            for token in tokens:
                assert entered[token].wait(5), 'Both actual token-bound external results held'
            before_all = execution.lp_auto_state()
            assert account.orders == original_orders and account.posts == []
            assert before_all['slots']['occupied'] == 5
            assert Decimal(before_all['funds']['buy_reserved_usd']) == 40
            by_token = {i['token_id']: i for i in before_all['intents'] if i['token_id'] in tokens}
            assert set(by_token) == set(tokens)
            assert len({i['session_id'] for i in by_token.values()}) == 2
            assert len({i['intent_id'] for i in by_token.values()}) == 2
            for token in tokens:
                before = execution.lp_auto_state()
                intent = by_token[token]
                sid = intent['session_id']
                assert intent['state'] == 'reserved' and Decimal(intent['reserved_usd']) == 8
                assert lp.entry_send_inflight(sid)
                assert account.orders == original_orders and account.posts == []
                if phase == 'after-sign':
                    assert signed_objects[token].token_id == token
                action, = [a for a in store.lp_actions(sid) if a['role'] == 'entry']
                pending_audits[token] = deepcopy(action)
                assert action['state'] == 'pending'
                assert action['submit_stage'] == 'preparing' and action['post_started'] is False
                assert action['token_id'] == token and action['session_id'] == sid
                assert store.lp_session(sid).get('post_started') is False
                assert store.lp_session(sid)['submit_stage'] == 'preparing'
                financial_as_of = before['funds']['as_of']
                assert financial_as_of
                if reconcile:
                    reconciled = lp.reconcile_facts(sid, apply_lock=(execution._acquire_global_lock,
                        execution._release_global_lock), account_round=rounds.get(token))
                    after = execution.lp_auto_state()
                    current = next(i for i in after['intents'] if i['intent_id'] == intent['intent_id'])
                    captures.append(dict(token=token, phase='ordinary-overlap', current=deepcopy(current),
                        result_error=reconciled[3], state=deepcopy(after)))
                    (store.path.parent / 'CI-D-overlap-held-proof.json').write_text(json.dumps(dict(
                        phase=phase, held=before_all, captures=captures, pending_audits=pending_audits),
                        default=str, indent=2))
                    assert current['state'] == 'reserved' and current['reserved_usd'] == intent['reserved_usd']
                    assert current['financial_status'] == 'known', (current, reconciled[3])
                    assert reconciled[3] is None and not current.get('reconcile_reason'), (current, reconciled[3])
                    assert after['slots']['occupied'] == before['slots']['occupied'] == 5
                    assert after['funds']['as_of'] == financial_as_of
                assert account.posts == [] and account.orders == original_orders
                assert all(next(i for i in execution.lp_auto_state()['intents'] if i['intent_id'] == held['intent_id'])['reserved_usd'] == held['reserved_usd'] for held in by_token.values())
                latest_financial_as_of.append(execution.lp_auto_state()['funds']['as_of'])
                assert latest_financial_as_of[-1]
            assert len({a['action_id'] for a in pending_audits.values()}) == 2
            assert len({a['action_key'] for a in pending_audits.values()}) == 2
        finally:
            for event in (*release.values(), *signing_release.values()):
                event.set()
            for account_round in rounds.values():
                adapter.lp_account_round_end(account_round)
        state = pending.result(timeout=5)
    assert len(account.orders) == state['slots']['occupied'] == 5
    assert Counter(p.token_id for p in account.posts) == Counter(tokens)
    assert all(p.post_only is True and p.maker_amount == 8000000 and p.taker_amount == 20000000 for p in account.posts)
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert datetime.fromisoformat(latest_financial_as_of[-1]) <= runtime.clock[0]
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['as_of'] == latest_financial_as_of[-1]
    accepted_audits = {}
    for token, before in pending_audits.items():
        action, = store.lp_actions(before['session_id'])
        assert action['state'] == 'accepted' and action['post_started'] is True
        assert action['action_id'] == before['action_id'] and action['action_key'] == before['action_key']
        assert action['token_id'] == token and action['session_id'] == before['session_id']
        accepted_audits[before['session_id']] = deepcopy(store.lp_actions(before['session_id']))
    _advance(runtime)
    assert execution.lp_auto_run_once(round_id='refill-two')['slots']['occupied'] == 5
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    assert execution.lp_auto_run_once(round_id='next-round')['slots']['occupied'] == 5
    assert Counter(p.token_id for p in account.posts) == Counter(tokens)
    assert account.cancels == account.market_orders == []
    assert {sid: store.lp_actions(sid) for sid in accepted_audits} == accepted_audits
    (store.path.parent / 'CI-D-two-native-proof.json').write_text(json.dumps(dict(
        phase=phase, reconcile=reconcile, held=before_all, captures=captures,
        pending_audits=pending_audits, accepted_audits=accepted_audits, final=state,
        current=execution.lp_auto_state(), post_tokens=[p.token_id for p in account.posts]), default=str, indent=2))


@pytest.mark.parametrize('fence, reason', [
    ('pause', 'manually_paused'), ('stop', 'session_stopped_before_send'),
    ('config', 'config_version_changed'),
])
def test_preparing_publication_keeps_send_fences(runtime, fence, reason):
    import json
    from copy import deepcopy
    from time import monotonic
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
            intent, = execution.lp_auto_state()['intents']
            sid = intent['session_id']
            original_audit, = deepcopy(store.lp_actions(sid))
            assert original_audit['state'] == 'pending' and original_audit['post_started'] is False
            lp.reconcile_facts(sid, apply_lock=(execution._acquire_global_lock, execution._release_global_lock))
            assert execution.lp_auto_state()['intents'][0]['state'] == 'reserved'
            if fence == 'pause':
                execution.lp_auto_set_desired_running(False)
                deadline = monotonic() + 5
                while True:
                    local = execution.lp_auto_state()
                    if local['active_plan'] is None and local['last_round']['completed_at']:
                        break
                    assert monotonic() < deadline, 'Public paused local closure watchdog'
                    Event().wait(.01)
                action, = local['last_round']['actions']
                assert action['state'] == 'not_sent' and action['reason'] == 'cycle_ended'
                assert lp.entry_send_inflight(sid), 'Held real SDK signature has not ended with local closure'
                assert account.posts == []
            elif fence == 'stop':
                execution.lp_stop(sid)
                assert store.lp_session(sid)['stop_requested'] is True
            else:
                # Existing durable-version fixture; public occupied config correctly refuses edits.
                execution._auto_pool._update(lambda d: d.update(config_version=d['config_version'] + 1, trading_config_version=d['trading_config_version'] + 1))
        finally:
            release.set()
        state = pending.result(timeout=5)
        deadline = monotonic() + 5
        while lp.entry_send_inflight(sid):
            assert monotonic() < deadline, 'Original native sender completion watchdog'
            Event().wait(.01)
    assert account.posts == account.cancels == []
    action, = state['last_round']['actions']
    assert action['reason'] == ('cycle_ended' if fence == 'pause' else 'prepare_failed')
    assert action['state'] == ('not_sent' if fence == 'pause' else 'rejected')
    assert state['active_plan'] is None and state['last_round']['completed_at']
    current = store.lp_session(sid)
    audit, = store.lp_actions(sid)
    assert current['idempotency_key'] == 'lp-auto:' + intent['intent_id']
    assert audit['action_id'] == original_audit['action_id'] and audit['action_key'] == original_audit['action_key']
    assert audit['token_id'] == original_audit['token_id'] and audit['session_id'] == sid
    assert audit['state'] == 'rejected'
    assert not current.get('entry_order_id') and not audit.get('order_id')
    for fact in (current, audit):
        assert fact['submit_stage'] == 'prepare_failed'
        assert fact['post_started'] is False and fact['submit_post_started_at'] is None
    (store.path.parent / 'CI-D-control-native-proof.json').write_text(json.dumps(dict(
        fence=fence, original_reason_label=reason, state=state, intent=intent,
        original_audit=original_audit, session=current, audit=audit), default=str, indent=2))


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
            financial_as_of = owner.lp_auto_state()['funds']['as_of']
        finally:
            release.set()
        pending.result(timeout=5)
    if uncertainty == 'read-error':
        assert len(account.posts) == 1 and account.cancels == []
        assert account.posts[0].token_id == intent['token_id']
        assert account.posts[0].maker_amount == 8000000 and account.posts[0].taker_amount == 20000000
        final = execution.lp_auto_state()
        assert final['funds']['status'] == 'unknown' and final['funds']['as_of'] == financial_as_of
        assert financial_as_of
    else:
        assert account.posts == account.cancels == []


@pytest.mark.parametrize('receipt', ['late', 'missing', 'idless'])
@pytest.mark.parametrize('stale_reserved', [False, True], ids=['sending-intent', 'stale-reserved-intent'])
def test_post_started_with_stale_preparing_action_keeps_unknown_until_receipt(runtime, receipt, stale_reserved):
    from datetime import datetime
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
            financial_as_of = execution.lp_auto_state()['funds']['as_of']
            assert financial_as_of and datetime.fromisoformat(str(financial_as_of).replace('Z', '+00:00'))
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
        assert state['funds']['status'] == 'unknown'
        assert state['funds']['as_of'] == financial_as_of
    else:
        assert not session['entry_order_id']
        assert session['submit_status'] == ('unknown' if receipt == 'missing' else 'accepted_without_order_id')
        assert action['state'] == ('unknown' if receipt == 'missing' else 'accepted')
        assert state['intents'][0]['state'] == 'unknown'
    execution.lp_auto_run_once(round_id='late-entry')
    assert len(account.posts) == 1
    assert account.cancels == account.market_orders == []
