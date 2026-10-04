"""Explicit operator authority waives a local hold, never exchange evidence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from threading import Event
from types import SimpleNamespace

import pytest
from polymarket.models.clob import SignedOrder

from open_trader.polymarket_lp_accounting import reservation_is_covered, reservation_is_manually_released
from tests.test_lp_account_reservation_reconciliation import (
    runtime, _advance, _amount, _seed_unknowns, _assert_unknown_audit,
    NOW, WALLET, CONDITION_ID, TOKEN_ID, NO_TOKEN_ID, _CandidateSDKPublic,
    _FiveMarketPublic, _refill_identity, _position, _open_order, _maker_order, _trade,
)
from tests.test_lp_auto_control import runtime_for
from tests.test_prediction_service import _server, _response, _production_request


def test_manual_single_release_overrides_missing_legacy_proof_preserves_audit(runtime):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 5})
    originals = _seed_unknowns(store, execution, ownership=False)
    audit = {row["session_id"]: deepcopy(store.lp_actions(row["session_id"])) for row in originals}
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()["state"] == "ready"
    assert execution.lp_auto_state()["slots"]["occupied"] == 2
    assert execution.lp_auto_state()["funds"]["status"] == "unknown"
    before_intents = deepcopy(execution._auto_pool._read()["intents"])
    wake = Event()
    execution.set_lp_auto_scheduler(SimpleNamespace(request_check=wake.set))
    listed = execution.lp_auto_reservations()
    assert all(row["eligible"] for row in listed["reservations"])
    result = execution.lp_auto_release_reservations(
        {"confirm": True, "intent_id": originals[0]["intent_id"], "reason": "operator accepts delayed order risk"},
        audit={"actor": "local_operator", "git_sha": "test-sha"},
    )
    assert [row["intent_id"] for row in result["released"]] == [originals[0]["intent_id"]]
    assert Decimal(result["released_amount_usd"]) == 8
    assert result["state"]["desired_running"] is False
    assert result["state"]["slots"]["occupied"] == 1
    assert _amount(result["state"], "buy_reserved_usd") == 8
    assert not wake.is_set(), "A paused pool remains paused"
    first = store.lp_session(originals[0]["session_id"])
    assert first["manual_reservation_release"]["actor"]["actor"] == "local_operator"
    assert first["manual_reservation_release"]["original_entry_actions"] == audit[first["session_id"]]
    assert first["manual_reservation_release"]["original_submit_status"] == "unknown"
    after_intents = execution._auto_pool._read()["intents"]
    assert {key: value for key, value in after_intents[originals[0]["intent_id"]].items()
            if key != "manual_reservation_release"} == before_intents[originals[0]["intent_id"]]
    assert not reservation_is_covered(first), "Operator authority is not an exchange snapshot"
    assert first["state"] == "complete"
    assert not execution._auto_pool._excluded(first["condition_id"])
    assert lp._candidate_reservations() == ({"order_id": f"lp-session:{originals[1]['session_id']}", "amount": Decimal("8")},)
    for row in originals:
        assert store.lp_actions(row["session_id"]) == audit[row["session_id"]]
    _assert_unknown_audit(store, execution, originals)
    assert account.posts == account.cancels == []


ROOT = '/api/prediction-arbitrage/lp/auto/reservations'


def _release(execution, **selection):
    return execution.lp_auto_release_reservations(
        {'confirm': True, 'reason': 'operator accepts uncertain historical outcome', **selection},
        audit={'actor': 'local_operator', 'git_sha': 'offline-test'},
    )


@pytest.mark.parametrize('unknown_amount', [False, True])
def test_manual_release_http_list_auth_csrf_and_strict_selection(runtime, tmp_path, unknown_amount):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    originals = _seed_unknowns(store, execution, ownership=False)
    if unknown_amount:
        execution._auto_pool._update(lambda doc: doc['intents'][originals[0]['intent_id']].update(reserved_usd=None))
    service = runtime_for(tmp_path / 'http')
    service.execution = execution
    service.store = store
    before = deepcopy(execution._auto_pool._read())
    sessions = store.lp_sessions()
    with _server(service, session_token='session-token', csrf_token='csrf-token') as base:
        status, listed = _response(base + ROOT)
        assert status == 200
        assert len(listed['reservations']) == 2
        row = listed['reservations'][0]
        assert row['intent_id'] == originals[0]['intent_id']
        assert row['session_id'] == originals[0]['session_id']
        assert row['amount_usd'] == (None if unknown_amount else '8')
        assert row['token_id'] == TOKEN_ID
        assert row['original_unknown_reason'] == 'missing_reliable_order_id'
        assert row['eligible'] is True and row['exclusion_reason'] is None
        valid = {'confirm': True, 'all_releasable': True, 'reason': 'explicit local waiver'}
        for headers in ({'Cookie': ''}, {'X-CSRF-Token': 'wrong'}, {'Origin': 'http://example.com'}, {'Host': 'example.com'}):
            assert _response(_production_request(base, ROOT + '/release', json.dumps(valid).encode(), headers=headers))[0] == 403
        invalid = [
            {**valid, 'confirm': False}, {**valid, 'confirm': 1},
            {**valid, 'intent_id': originals[0]['intent_id']},
            {'confirm': True, 'reason': 'no selection'},
            {**valid, 'all_releasable': False}, {**valid, 'all_releasable': 1},
            {**valid, 'reason': ''}, {**valid, 'reason': 42}, {**valid, 'reason': 'x' * 501},
            {**valid, 'force': True}, {'confirm': True, 'intent_id': [], 'reason': 'bad id'},
        ]
        for payload in invalid:
            assert _response(_production_request(base, ROOT + '/release', json.dumps(payload).encode()))[0] == 400
        assert execution._auto_pool._read() == before
        assert store.lp_sessions() == sessions
        status, result = _response(_production_request(base, ROOT + '/release', json.dumps(valid).encode()))
        assert status == 200 and len(result['released']) == 2
        assert result['released_amount_usd'] == (None if unknown_amount else '16')
        returned = next(r for r in result['released'] if r['intent_id'] == originals[0]['intent_id'])
        assert returned['amount_usd'] == (None if unknown_amount else '8')
        assert result['state']['funds']['available_usd'] is None
        assert result['state']['funds']['spendable_usd'] is None
        assert result['state']['desired_running'] is False
        for original in originals:
            marker = store.lp_session(original['session_id'])['manual_reservation_release']
            assert marker['actor']['actor'] == 'local_operator'
            assert marker['selection'] == {'all_releasable': True}
        service._owner.held = False
        assert _response(base + ROOT)[0] == 503
        assert _response(_production_request(base, ROOT + '/release', json.dumps(valid).encode()))[0] == 503
        service._owner.held = True
        service.execution = None
        assert _response(base + ROOT)[0] == 503
        assert _response(_production_request(base, ROOT + '/release', json.dumps(valid).encode()))[0] == 503
        service.execution = execution
    service._owner.held = True
    shadow = SimpleNamespace(mode='shadow', state='RUNNING', shadow_evidence={'mode': 'shadow', 'first_violation': None}, execution=execution)
    with _server(shadow) as base:
        assert _response(base + ROOT)[0] == 200
        assert _response(_production_request(base, ROOT + '/release', json.dumps(valid).encode()))[0] == 403
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('independent_key,role', [('independent-exit', 'protected_exit'), ('entry-cancel', 'entry'), ('legacy-exit', None)])
def test_manual_all_releases_only_eligible_preserves_known_account_and_inventory(runtime, independent_key, role):
    order = _open_order('actual-buy', 'BUY', price='0.40', original='40', matched='20', token_id=NO_TOKEN_ID, outcome='NO')
    maker = _maker_order('actual-buy', 'BUY', '20', '0.40').model_copy(update={'token_id': NO_TOKEN_ID, 'outcome': 'NO'})
    fill = _trade('actual-fill', maker, size='20').model_copy(update={'token_id': NO_TOKEN_ID, 'outcome': 'NO'})
    position = _position().model_copy(update={'token_id': NO_TOKEN_ID, 'outcome': 'NO'})
    store, adapter, account, lp, execution = runtime(orders=(order,), trades=(fill,), positions=(position,))
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    originals = _seed_unknowns(store, execution, ownership=False, count=3)
    store.lp_update_session(originals[1]['session_id'], patch={'account_id': '0x' + 'f' * 40})
    if role is None:
        store.lp_update_session(originals[2]['session_id'], patch={'protected_exit_attempt_state': 'unknown'})
    else:
        store.lp_upsert_action(originals[2]['session_id'], independent_key, state='unknown',
            payload={'role': role, 'side': 'SELL'})
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    known = next(row for row in store.lp_sessions() if 'actual-buy' in row.get('owned_order_ids', []))
    before_orders = deepcopy(known['order_history'])
    result = _release(execution, all_releasable=True)
    assert [row['intent_id'] for row in result['released']] == [originals[0]['intent_id']]
    assert {row['intent_id']: row['reason'] for row in result['skipped']} == {
        originals[1]['intent_id']: 'foreign_account', originals[2]['intent_id']: 'independent_action_unresolved',
    }
    assert store.lp_session(known['session_id'])['order_history'] == before_orders
    assert _amount(result['state'], 'inventory_cost_usd') == 8
    assert _amount(result['state'], 'buy_reserved_usd') == 24  # Actual 8 + two untouched holds.
    assert result['state']['slots']['occupied'] == 3
    assert not store.lp_session(originals[1]['session_id']).get('manual_reservation_release')
    assert not store.lp_session(originals[2]['session_id']).get('manual_reservation_release')
    assert account.posts == account.cancels == account.market_orders == []


def test_manual_release_restart_reconcile_and_late_receipt_do_not_restore_hold(runtime):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    originals = _seed_unknowns(store, execution, ownership=False, count=1)
    sid, iid = originals[0]['session_id'], originals[0]['intent_id']
    audit = deepcopy(store.lp_actions(sid))
    first = _release(execution, intent_id=iid)
    marker = deepcopy(store.lp_session(sid)['manual_reservation_release'])
    assert first['state']['slots']['occupied'] == 0
    assert first['state']['funds']['status'] == 'unknown'  # Waiver creates no account proof.
    repeat = _release(execution, intent_id=iid)
    assert repeat['released'] == [] and repeat['released_amount_usd'] == '0'
    assert [row['intent_id'] for row in repeat['already_released']] == [iid]
    assert store.lp_session(sid)['manual_reservation_release'] == marker
    # A late local hook must not revive the empty request container exclusion.
    store.lp_update_session(sid, state='needs_attention', patch={'facts_error': 'missing_reliable_order_id'})
    execution._auto_pool._record_session(iid, store.lp_session(sid), error='submission_unknown')
    store.lp_register_trade_change(sid)
    assert not execution._auto_pool._excluded(CONDITION_ID)
    assert lp._candidate_reservations() == ()
    adapter.close()
    _advance(runtime)
    order = _open_order('late-buy', 'BUY', price='0.40', original='20')
    store, adapter, account, lp, execution = runtime(orders=(order,))
    state = execution.lp_auto_reconcile_unknown()
    assert state['slots']['occupied'] == 1
    assert _amount(state, 'buy_reserved_usd') == 8
    assert state['funds']['status'] == 'known'
    assert any('late-buy' in row.get('owned_order_ids', []) for row in store.lp_sessions())
    for _ in range(2):
        _advance(runtime)
        assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        assert _amount(execution.lp_auto_state(), 'buy_reserved_usd') == 8
        assert store.lp_session(sid)['manual_reservation_release'] == marker
        assert not reservation_is_covered(store.lp_session(sid))
        assert store.lp_actions(sid) == audit
        _assert_unknown_audit(store, execution, originals, managed_order_id=store.lp_session(sid).get('entry_order_id'))
    assert _release(execution, all_releasable=True)['released'] == []
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('reserved_amount', ['8', None])
def test_manual_release_wakes_enabled_pool_and_normal_sdk_refill(runtime, reserved_amount):
    store, adapter, account, lp, execution = runtime(public_client=_CandidateSDKPublic(NOW))
    execution.lp_auto_configure({'budget_usd': '8', 'target_buy_count': 1})
    originals = _seed_unknowns(store, execution, ownership=False, count=1)
    iid = originals[0]['intent_id']
    execution._auto_pool._update(lambda doc: doc['intents'][iid].update(reserved_usd=reserved_amount))
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    assert execution.lp_auto_state()['funds']['status'] == 'unknown'
    assert not store.lp_session(originals[0]['session_id']).get('reservation_coverage')
    wake = Event()
    execution.set_lp_auto_scheduler(SimpleNamespace(request_check=wake.set))
    execution.lp_auto_set_desired_running(True)
    result = _release(execution, intent_id=originals[0]['intent_id'])
    assert wake.is_set()
    assert result['released_amount_usd'] == reserved_amount
    marker = store.lp_session(originals[0]['session_id'])['manual_reservation_release']
    assert marker['amount_usd'] == marker['original_reserved_usd'] == reserved_amount
    assert execution._auto_pool._read()['intents'][iid]['reserved_usd'] == reserved_amount
    assert result['state']['desired_running'] is True
    assert result['state']['slots']['occupied'] == 0
    assert result['state']['funds']['status'] == 'known'
    facts = lp._read_candidate_facts({'condition_id': CONDITION_ID, 'token_id': TOKEN_ID, 'outcome': 'YES'})
    lp._candidate_pool_record_success(CONDITION_ID, {'condition_id': CONDITION_ID}, judged_at=lp._now(),
        facts={'directions': [facts['direction']], 'account': facts['account']})
    store.lp_save_price_history(CONDITION_ID, TOKEN_ID, [], {
        'state': 'known', 'amplitude': Decimal('.005'), 'checked_at': lp._now(),
        'valid_until': lp._now() + timedelta(days=1),
    })
    def signed_order(**kwargs):
        _advance(runtime)
        return SignedOrder(builder='0x1', expiration=kwargs['expiration'], maker=WALLET,
            maker_amount=8000000, metadata='0x3', order_type='GTD', salt=1,
            side='BUY', signature='0x4', signature_type=0, signer=WALLET,
            taker_amount=20000000, timestamp=1, token_id=str(kwargs['token_id']), post_only=True)
    def accepted_post(signed):
        _advance(runtime)
        account.posts.append(signed)
        account.orders = (_open_order('manual-waiver-refill', 'BUY', price='0.40', original='20'),)
        return {'order_id': 'manual-waiver-refill', 'status': 'LIVE', 'accepted': True, 'size_matched': '0'}
    account.create_limit_order = signed_order
    account.post_order = accepted_post
    refilled = execution.lp_auto_run_once(round_id='refill-after-manual-waiver')
    assert len(account.posts) == 1, refilled['last_round']
    assert refilled['slots']['occupied'] == 1
    _advance(runtime)
    known = execution.lp_auto_run_once(round_id='confirm-manual-refill')
    assert known['funds']['status'] == 'known'
    assert _amount(known, 'available_usd') == 0
    assert len(account.posts) == 1
    _assert_unknown_audit(store, execution, originals)
    assert account.cancels == account.market_orders == []


@pytest.mark.parametrize('stage', ['preparing', 'sending'])
def test_stale_durable_send_stage_does_not_make_legacy_hold_permanent(runtime, stage):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    originals = _seed_unknowns(store, execution, ownership=False, count=1, stage=stage)
    sid = originals[0]['session_id']
    assert not lp.entry_send_inflight(sid)
    audit = store.lp_actions(sid)
    assert execution.lp_auto_reservations()['reservations'][0]['eligible'] is True
    result = _release(execution, intent_id=originals[0]['intent_id'])
    assert len(result['released']) == 1
    assert result['state']['slots']['occupied'] == 0
    assert lp._candidate_reservations() == ()
    assert not lp._has_unresolved_submission(store.lp_session(sid))
    assert store.lp_actions(sid) == audit
    assert store.lp_session(sid)['submit_stage'] == stage
    assert account.posts == account.cancels == []


def test_real_sdk_post_inflight_is_excluded_until_actual_send_finishes(runtime, monkeypatch):
    public = _FiveMarketPublic(runtime.clock)
    store, adapter, account, lp, execution = runtime(public_client=public)
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    market, condition, token = _refill_identity(2)
    facts = lp._read_candidate_facts({'market_id': market, 'condition_id': condition, 'token_id': token, 'outcome': 'YES'})
    lp._candidate_pool_record_success(condition, {'condition_id': condition}, judged_at=lp._now(),
        facts={'directions': [facts['direction']], 'account': facts['account']})
    store.lp_save_price_history(condition, token, [], {
        'state': 'known', 'amplitude': Decimal('.005'), 'checked_at': lp._now(),
        'valid_until': lp._now() + timedelta(days=1),
    })
    entered, finish, selected = Event(), Event(), Event()
    def sign(**kwargs):
        _advance(runtime)
        return SignedOrder(builder='0x1', expiration=kwargs['expiration'], maker=WALLET,
            maker_amount=8000000, metadata='0x3', order_type='GTD', salt=1,
            side='BUY', signature='0x4', signature_type=0, signer=WALLET,
            taker_amount=20000000, timestamp=1, token_id=str(kwargs['token_id']), post_only=True)
    def post(signed):
        account.posts.append(signed)
        entered.set()
        assert finish.wait(10), 'POST release watchdog expired'
        raise TimeoutError('offline lost POST receipt')
    account.create_limit_order = sign
    account.post_order = post
    execution.lp_auto_set_desired_running(True)
    read = execution._auto_pool.reservations
    def watched_selection():
        result = read()
        selected.set()
        return result
    with ThreadPoolExecutor(max_workers=2) as workers:
        sending = workers.submit(execution.lp_auto_run_once, round_id='live-send')
        try:
            assert entered.wait(10), 'The real send never reached POST'
            live = next(row for row in store.lp_sessions() if row['idempotency_key'] == 'lp-auto:live-send:0')
            assert lp.entry_send_inflight(live['session_id'])
            # Even if a legacy callback classified the durable intent UNKNOWN,
            # the actual in-process POST must remain excluded.
            execution._auto_pool._update(lambda doc: doc['intents']['live-send:0'].update(state='unknown'))
            old = _seed_unknowns(store, execution, ownership=False, count=1)[0]
            listed = {row['intent_id']: row for row in read()['reservations']}
            assert listed['live-send:0']['exclusion_reason'] == 'send_inflight'
            assert listed[old['intent_id']]['eligible'] is True
            monkeypatch.setattr(execution._auto_pool, 'reservations', watched_selection)
            release = workers.submit(_release, execution, all_releasable=True)
            assert selected.wait(10), 'Release did not capture the in-flight exclusion'
        finally:
            finish.set()
        ended = sending.result(timeout=10)
        result = release.result(timeout=10)
    # The existing send barrier allows publication after POST returns. The
    # excluded send cannot become selected just because it ended meanwhile.
    assert [row['intent_id'] for row in result['released']] == [old['intent_id']]
    assert {row['intent_id']: row['reason'] for row in result['skipped']} == {'live-send:0': 'send_inflight'}
    assert not store.lp_session(live['session_id']).get('manual_reservation_release')
    assert result['state']['slots']['occupied'] == 1
    assert not lp.entry_send_inflight(live['session_id'])
    assert _release(execution, intent_id='live-send:0')['released']
    assert len(account.posts) == 1
    assert account.cancels == []


@pytest.mark.parametrize('change', ['session-version', 'reservation-version', 'reservation-unknown', 'new-request'])
def test_manual_all_fences_stale_selection_and_never_clears_new_request(runtime, monkeypatch, change):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    originals = _seed_unknowns(store, execution, ownership=False)
    pool = execution._auto_pool
    selected, resume = Event(), Event()
    read = pool.reservations
    def observed_selection():
        result = read()
        selected.set()
        assert resume.wait(10), 'Selection release watchdog expired'
        return result
    monkeypatch.setattr(pool, 'reservations', observed_selection)
    with ThreadPoolExecutor(max_workers=1) as workers:
        release = workers.submit(_release, execution, all_releasable=True)
        try:
            assert selected.wait(10), 'Selection was not captured'
            if change == 'session-version':
                store.lp_update_session(originals[0]['session_id'], patch={'new_observation': True})
            elif change in {'reservation-version', 'reservation-unknown'}:
                pool._update(lambda doc: doc['intents'][originals[0]['intent_id']].update(
                    reserved_usd=None if change == 'reservation-unknown' else '9'))
            else:
                sid = 'new-unknown-session'
                payload = {key: store.lp_session(originals[0]['session_id'])[key] for key in
                           ('market_id', 'condition_id', 'token_id', 'outcome', 'price', 'quantity', 'submit_status')}
                payload.update(token_id=NO_TOKEN_ID, outcome='NO')
                store.lp_create_session(sid, 'unproven:new-request', state='needs_attention', payload=payload)
                intent = {**originals[0], 'intent_id': 'new-request:0', 'session_id': sid, 'token_id': NO_TOKEN_ID, 'outcome': 'NO'}
                pool._update(lambda doc: doc['intents'].update({'new-request:0': intent}))
        finally:
            resume.set()
        result = release.result(timeout=10)
    if change == 'new-request':
        assert len(result['released']) == 2
        assert not store.lp_session(sid).get('manual_reservation_release')
        assert result['state']['slots']['occupied'] == 1
    else:
        assert [row['intent_id'] for row in result['released']] == [originals[1]['intent_id']]
        expected = 'session_changed' if change == 'session-version' else 'reservation_changed'
        assert {row['intent_id']: row['reason'] for row in result['skipped']} == {originals[0]['intent_id']: expected}
        assert not store.lp_session(originals[0]['session_id']).get('manual_reservation_release')
    assert account.posts == account.cancels == []


def test_manual_release_failure_rolls_back_markers_sessions_and_audit(runtime, monkeypatch):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    originals = _seed_unknowns(store, execution, ownership=False)
    pool = execution._auto_pool
    before = deepcopy(pool._read())
    sessions = deepcopy(store.lp_sessions())
    event = pool._event
    def fail_second(document, intent, kind, **kwargs):
        if kind == 'manual_reservation_release' and intent['intent_id'] == originals[1]['intent_id']:
            raise OSError('injected audit write failure')
        return event(document, intent, kind, **kwargs)
    monkeypatch.setattr(pool, '_event', fail_second)
    with pytest.raises(OSError, match='injected audit write failure'):
        _release(execution, all_releasable=True)
    assert pool._read() == before
    assert store.lp_sessions() == sessions
    assert not any(reservation_is_manually_released(row) for row in store.lp_sessions())
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('reserved_amount', ['8', None])
@pytest.mark.parametrize('cost, residual', [('4.20', '10'), (None, None)])
def test_manual_waiver_never_invents_inventory_profit_or_zero_quantity(runtime, cost, residual, reserved_amount):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    row = _seed_unknowns(store, execution, ownership=False, count=1)[0]
    execution._auto_pool._update(lambda doc: doc['intents'][row['intent_id']].update(inventory_cost_usd=cost, reserved_usd=reserved_amount))
    store.lp_update_session(row['session_id'], patch={'residual_quantity': residual, 'buy_cost': cost})
    before = store.lp_session(row['session_id'])
    result = _release(execution, intent_id=row['intent_id'])
    assert result['released']
    assert result['released_amount_usd'] == reserved_amount
    assert result['state']['funds']['status'] == 'unknown'
    assert result['state']['funds']['spendable_usd'] is None
    assert result['state']['slots']['occupied'] == 0
    assert _amount(result['state'], 'buy_reserved_usd') == 0
    if cost is not None:
        assert _amount(result['state'], 'inventory_cost_usd') == Decimal('4.20')
    else:
        assert result['state']['funds']['inventory_cost_usd'] is None
    after = store.lp_session(row['session_id'])
    for key in ('residual_quantity', 'buy_filled_quantity', 'buy_cost', 'fees', 'fee_status', 'submit_status'):
        assert after[key] == before[key]
    assert not after.get('position_reconciled')
    assert account.posts == account.cancels == []


def test_late_explicit_exchange_id_reuses_retired_manual_container_without_fake_proof(runtime):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    row = _seed_unknowns(store, execution, ownership=False, count=1)[0]
    sid = row['session_id']
    audit = deepcopy(store.lp_actions(sid))
    _release(execution, intent_id=row['intent_id'])
    marker = deepcopy(store.lp_session(sid)['manual_reservation_release'])
    store.lp_register_exchange_orders(WALLET, TOKEN_ID, [{
        'order_id': 'explicit-late-id', 'token_id': TOKEN_ID, 'condition_id': CONDITION_ID,
        'side': 'BUY', 'status': 'LIVE', 'price': '.40', 'original_size': '20',
        'size_matched': '0', 'remaining_size': '20',
    }], session=store.lp_session(sid))
    returned = store.lp_session(sid)
    assert returned['state'] == 'entry_open'
    assert returned['manual_reservation_release'] == marker
    assert returned['submit_status'] == 'unknown'
    assert returned['owned_order_ids'] == ['explicit-late-id']
    assert not reservation_is_covered(returned)
    execution._auto_pool._record_session(row['intent_id'], returned)
    assert _amount(execution.lp_auto_state(), 'buy_reserved_usd') == 0  # Waived local request, no fresh account yet.
    assert execution.lp_auto_state()['funds']['status'] == 'unknown'
    account.orders = (_open_order('explicit-late-id', 'BUY', price='0.40', original='20'),)
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = execution.lp_auto_state()
    assert state['slots']['occupied'] == 1
    assert _amount(state, 'buy_reserved_usd') == 8
    assert state['funds']['status'] == 'known'
    assert _release(execution, intent_id=row['intent_id'])['already_released']
    assert store.lp_actions(sid) == audit
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('exclusion', ['known-order', 'foreign-pool', 'duplicate-binding'])
def test_manual_release_keeps_known_orders_and_account_bindings(runtime, monkeypatch, exclusion):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    row = _seed_unknowns(store, execution, ownership=False, count=1)[0]
    if exclusion == 'known-order':
        store.lp_update_session(row['session_id'], patch={'entry_order_id': 'known-real-order', 'owned_order_ids': ['known-real-order']})
    elif exclusion == 'foreign-pool':
        monkeypatch.setattr(lp, 'exchange', SimpleNamespace(config=SimpleNamespace(wallet_address='0x' + 'f' * 40)))
    else:
        execution._auto_pool._update(lambda doc: doc['intents'].update({'duplicate': {**row, 'intent_id': 'duplicate'}}))
    before = deepcopy(execution._auto_pool._read())
    sessions = deepcopy(store.lp_sessions())
    result = _release(execution, intent_id=row['intent_id'])
    assert result['released'] == []
    reason = {'known-order': 'order_identity_known', 'foreign-pool': 'account_identity_unknown',
              'duplicate-binding': 'session_binding_conflict'}[exclusion]
    assert result['skipped'][0]['reason'] == reason
    assert execution._auto_pool._read() == before
    assert store.lp_sessions() == sessions
    assert account.posts == account.cancels == []


def test_single_manual_release_returns_unknown_for_other_incomplete_inventory(runtime):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    rows = _seed_unknowns(store, execution, ownership=False)
    execution._auto_pool._update(lambda doc: [intent.update(inventory_cost_usd=None) for intent in doc['intents'].values()])
    result = _release(execution, intent_id=rows[0]['intent_id'])
    assert len(result['released']) == 1
    assert result['state']['slots']['occupied'] == 1
    assert result['state']['funds']['inventory_cost_usd'] is None
    assert result['state']['funds']['status'] == 'unknown'
    assert result['state']['funds']['spendable_usd'] is None
    assert not store.lp_session(rows[1]['session_id']).get('manual_reservation_release')


def test_manual_waiver_preserves_loss_report_without_changing_fixed_budget(runtime):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    row = _seed_unknowns(store, execution, ownership=False, count=1)[0]
    execution._auto_pool._update(lambda doc: doc['intents'][row['intent_id']].update(realized_pnl_usd='-1.40'))
    result = _release(execution, intent_id=row['intent_id'])
    assert _amount(result['state'], 'total_usd') == Decimal('100')
    assert _amount(result['state'], 'realized_pnl_usd') == Decimal('-1.40')
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = execution.lp_auto_state()
    assert state['funds']['status'] == 'known'
    assert _amount(state, 'total_usd') == Decimal('100')
    assert _amount(state, 'available_usd') == 100
    assert _amount(state, 'realized_pnl_usd') == Decimal('-1.40')
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('callback', ['session-update', 'fact-publication'])
def test_retired_manual_request_container_cannot_revive_without_actual_exposure(runtime, callback):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    row = _seed_unknowns(store, execution, ownership=False, count=1)[0]
    sid = row['session_id']
    _release(execution, intent_id=row['intent_id'])
    marker = deepcopy(store.lp_session(sid)['manual_reservation_release'])
    if callback == 'session-update':
        store.lp_update_session(sid, state='needs_attention', patch={'facts_error': 'missing_reliable_order_id'})
    else:
        assert store.lp_publish_facts(sid, store.lp_session_revision(sid, trading=True), state='needs_attention',
                                      patch={'facts_error': 'missing_reliable_order_id'}) is not None
    assert store.lp_session(sid)['state'] == 'complete'
    assert store.lp_session(sid)['manual_reservation_release'] == marker
    assert lp._lp_market_conflict(CONDITION_ID, 'YES') is None
    assert lp._candidate_reservations() == ()
    assert execution.lp_auto_state()['slots']['occupied'] == 0
    assert not reservation_is_covered(store.lp_session(sid))
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('account_field', ['account_id', 'wallet_address'])
def test_operator_waiver_does_not_follow_a_changed_foreign_account(runtime, account_field):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    row = _seed_unknowns(store, execution, ownership=False, count=1)[0]
    _release(execution, intent_id=row['intent_id'])
    marker = deepcopy(store.lp_session(row['session_id'])['manual_reservation_release'])
    store.lp_update_session(row['session_id'], patch={account_field: '0x' + 'f' * 40})
    changed = store.lp_session(row['session_id'])
    assert not reservation_is_manually_released(changed), 'A local waiver cannot authorize a foreign account'
    assert changed['manual_reservation_release'] == marker
    assert _release(execution, intent_id=row['intent_id'])['skipped'][0]['reason'] == 'foreign_account'
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('intent_state,stage', [('reserved', 'preparing'), ('sending', 'sending')])
def test_restart_stale_durable_entry_state_can_be_manually_waived(runtime, intent_state, stage):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    row = _seed_unknowns(store, execution, ownership=False, count=1, stage=stage)[0]
    sid, iid = row['session_id'], row['intent_id']
    execution._auto_pool._update(lambda doc: doc['intents'][iid].update(state=intent_state))
    store.lp_update_session(sid, state='entry_submit_pending', patch={'submit_status': 'pending'})
    audit = deepcopy(store.lp_actions(sid))
    original = deepcopy(execution._auto_pool._read()['intents'][iid])
    adapter.close()
    _advance(runtime)
    store, adapter, account, lp, execution = runtime()
    assert not lp.entry_send_inflight(sid)
    listed = execution.lp_auto_reservations()['reservations'][0]
    assert listed['eligible'] is True and listed['exclusion_reason'] is None
    result = _release(execution, intent_id=iid)
    assert [r['intent_id'] for r in result['released']] == [iid]
    assert result['state']['slots']['occupied'] == 0
    assert result['state']['funds']['status'] == 'unknown'
    assert lp._candidate_reservations() == ()
    assert lp._lp_market_conflict(CONDITION_ID, 'YES') is None
    assert not lp._has_unresolved_submission(store.lp_session(sid))
    assert store.lp_session(sid)['submit_stage'] == stage
    assert store.lp_session(sid)['submit_status'] == 'pending'
    assert store.lp_actions(sid) == audit
    after = execution._auto_pool._read()['intents'][iid]
    assert {k: v for k, v in after.items() if k != 'manual_reservation_release'} == original
    assert not reservation_is_covered(store.lp_session(sid))
    assert _release(execution, intent_id=iid)['already_released']
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('selection', ['single', 'all'])
def test_missing_local_amount_operator_waiver_preserves_null_audit_and_summary(runtime, selection):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    rows = _seed_unknowns(store, execution, ownership=False)
    iid, sid = rows[1]['intent_id'], rows[1]['session_id']
    execution._auto_pool._update(lambda doc: doc['intents'][iid].update(reserved_usd=None))
    audit = deepcopy(store.lp_actions(sid))
    listed = {r['intent_id']: r for r in execution.lp_auto_reservations()['reservations']}
    assert listed[iid]['eligible'] is True and listed[iid]['amount_usd'] is None
    result = _release(execution, **({'intent_id': iid} if selection == 'single' else {'all_releasable': True}))
    assert result['released_amount_usd'] is None
    assert {r['intent_id']: r['amount_usd'] for r in result['released']} == (
        {iid: None} if selection == 'single' else {rows[0]['intent_id']: '8', iid: None})
    assert result['state']['slots']['occupied'] == (1 if selection == 'single' else 0)
    assert result['state']['funds']['status'] == 'unknown'
    assert result['state']['funds']['spendable_usd'] is None
    saved = execution._auto_pool._read()['intents'][iid]
    marker = deepcopy(saved['manual_reservation_release'])
    assert saved['reserved_usd'] is None
    assert marker['original_reserved_usd'] is marker['amount_usd'] is None
    assert store.lp_session(sid)['manual_reservation_release'] == marker
    assert store.lp_actions(sid) == audit
    assert not reservation_is_covered(store.lp_session(sid))
    adapter.close()
    _advance(runtime)
    store, adapter, account, lp, execution = runtime()
    again = _release(execution, intent_id=iid)
    assert again['released'] == [] and again['released_amount_usd'] == '0'
    assert again['already_released'][0]['amount_usd'] is None
    assert execution._auto_pool._read()['intents'][iid]['reserved_usd'] is None
    assert store.lp_session(sid)['manual_reservation_release'] == marker
    assert store.lp_actions(sid) == audit
    assert account.posts == account.cancels == []


@pytest.mark.parametrize('phase', ['signing', 'queued'])
def test_actual_sdk_entry_preparation_and_send_queue_remain_excluded(runtime, monkeypatch, phase):
    public = _FiveMarketPublic(runtime.clock)
    store, adapter, account, lp, execution = runtime(public_client=public)
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    market, condition, token = _refill_identity(2)
    facts = lp._read_candidate_facts({'market_id': market, 'condition_id': condition, 'token_id': token, 'outcome': 'YES'})
    lp._candidate_pool_record_success(condition, {'condition_id': condition}, judged_at=lp._now(),
        facts={'directions': [facts['direction']], 'account': facts['account']})
    store.lp_save_price_history(condition, token, [], {
        'state': 'known', 'amplitude': Decimal('.005'), 'checked_at': lp._now(),
        'valid_until': lp._now() + timedelta(days=1),
    })
    entered, finish = Event(), Event()
    def sign(**kwargs):
        if phase == 'signing':
            entered.set()
            assert finish.wait(10), 'Signing watchdog expired'
        _advance(runtime)
        return SignedOrder(builder='0x1', expiration=kwargs['expiration'], maker=WALLET,
            maker_amount=8000000, metadata='0x3', order_type='GTD', salt=1,
            side='BUY', signature='0x4', signature_type=0, signer=WALLET,
            taker_amount=20000000, timestamp=1, token_id=str(kwargs['token_id']), post_only=True)
    def post(signed):
        account.posts.append(signed)
        raise TimeoutError('offline lost POST receipt')
    account.create_limit_order, account.post_order = sign, post
    execution.lp_auto_set_desired_running(True)
    barrier = execution._auto_pool._send_barrier
    @contextmanager
    def observed_barrier():
        entered.set()
        with barrier():
            yield
    if phase == 'queued':
        monkeypatch.setattr(execution._auto_pool, '_send_barrier', observed_barrier)
    with ThreadPoolExecutor(max_workers=1) as workers:
        # The queued case waits on the real file lock, not a durable stage flag.
        with barrier() if phase == 'queued' else nullcontext():
            sending = workers.submit(execution.lp_auto_run_once, round_id='preparing-send')
            try:
                assert entered.wait(10), 'Actual entry operation did not reach the controlled phase'
                live = next(row for row in store.lp_sessions() if row['idempotency_key'] == 'lp-auto:preparing-send:0')
                assert account.posts == []
                assert lp.entry_send_inflight(live['session_id'])
                row = execution.lp_auto_reservations()['reservations'][0]
                assert row['intent_id'] == 'preparing-send:0'
                assert not row['eligible'] and row['exclusion_reason'] == 'send_inflight'
                assert not store.lp_session(live['session_id']).get('manual_reservation_release')
            finally:
                finish.set()
        sending.result(timeout=10)
    assert not lp.entry_send_inflight(live['session_id'])
    assert len(_release(execution, intent_id='preparing-send:0')['released']) == 1
    assert len(account.posts) == 1 and account.cancels == []


@pytest.mark.parametrize('current_exposure', ['empty', 'inventory', 'open-buy', 'independent-sell'])
def test_manual_waiver_current_api_overrides_stale_local_inventory_for_refill(runtime, current_exposure):
    public = _FiveMarketPublic(runtime.clock)
    positions = (_position(),) if current_exposure == 'inventory' else ()
    store, adapter, account, lp, execution = runtime(public_client=public, positions=positions,
        orders=(_open_order('actual-open-buy', 'BUY', price='0.40', original='20'),) if current_exposure == 'open-buy' else ())
    execution.lp_auto_configure({'budget_usd': '8', 'target_buy_count': 1})
    original = _seed_unknowns(store, execution, ownership=False, count=1)[0]
    sid, iid = original['session_id'], original['intent_id']
    store.lp_update_session(sid, patch={'buy_filled_quantity': '20', 'residual_quantity': '20', 'buy_cost': '8'})
    execution._auto_pool._update(lambda doc: doc['intents'][iid].update(inventory_cost_usd='8'))
    audit = deepcopy(store.lp_actions(sid))
    if current_exposure == 'empty':
        _advance(runtime)
        assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    _release(execution, intent_id=iid)
    if current_exposure == 'empty':
        assert store.lp_session(sid)['state'] == 'complete', 'Reuse valid published API facts without requiring a second synchronization'
        assert not execution._auto_pool._excluded(CONDITION_ID)
    else:
        assert store.lp_session(sid)['state'] == 'needs_attention', 'Local history alone cannot prove current zero exposure'
    if current_exposure == 'independent-sell':
        store.lp_update_session(sid, patch={'protected_exit_attempt_state': 'unknown'})
        store.lp_upsert_action(sid, 'independent-exit', state='unknown', payload={'role': 'protected_exit', 'side': 'SELL'})
        audit = deepcopy(store.lp_actions(sid))
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    state = execution.lp_auto_state()
    assert state['funds']['status'] == 'known'
    assert _amount(state, 'total_usd') == 8
    assert _amount(state, 'inventory_cost_usd') == (8 if current_exposure == 'inventory' else 0)
    assert execution._auto_pool._excluded(CONDITION_ID) is (current_exposure != 'empty')
    if current_exposure == 'empty':
        store.lp_update_session(sid, state='needs_attention', patch={'facts_error': 'missing_reliable_order_id'})
        assert store.lp_session(sid)['state'] == 'complete', 'Late local hooks cannot revive an API-empty waived container'
        assert not execution._auto_pool._excluded(CONDITION_ID)
    retained = store.lp_session(sid)
    assert retained['buy_filled_quantity'] == retained['residual_quantity'] == '20'
    assert retained['buy_cost'] == '8'
    assert retained['submit_status'] == 'unknown'
    assert store.lp_actions(sid) == audit
    assert not reservation_is_covered(retained)
    facts = lp._read_candidate_facts({'condition_id': CONDITION_ID, 'token_id': TOKEN_ID, 'outcome': 'YES'})
    lp._candidate_pool_record_success(CONDITION_ID, {'condition_id': CONDITION_ID}, judged_at=lp._now(),
        facts={'directions': [facts['direction']], 'account': facts['account']})
    store.lp_save_price_history(CONDITION_ID, TOKEN_ID, [], {
        'state': 'known', 'amplitude': Decimal('.005'), 'checked_at': lp._now(),
        'valid_until': lp._now() + timedelta(days=1),
    })
    def sign(**kwargs):
        _advance(runtime)
        return SignedOrder(builder='0x1', expiration=kwargs['expiration'], maker=WALLET,
            maker_amount=8000000, metadata='0x3', order_type='GTD', salt=1,
            side='BUY', signature='0x4', signature_type=0, signer=WALLET,
            taker_amount=20000000, timestamp=1, token_id=str(kwargs['token_id']), post_only=True)
    def post(signed):
        _advance(runtime)
        account.posts.append(signed)
        account.orders = (_open_order('stale-history-refill', 'BUY', price='0.40', original='20'),)
        return {'order_id': 'stale-history-refill', 'status': 'LIVE', 'accepted': True, 'size_matched': '0'}
    account.create_limit_order, account.post_order = sign, post
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once(round_id='stale-history-refill')
    assert len(account.posts) == (1 if current_exposure == 'empty' else 0)
    assert store.lp_session(sid)['buy_filled_quantity'] == '20'
    assert store.lp_actions(sid) == audit
    assert account.cancels == account.market_orders == []


@pytest.mark.parametrize('authority', ['manual', 'automatic'])
@pytest.mark.parametrize('cost_basis,cost', [('initial-value', '4'), ('average-price', '8')])
def test_release_fixed_budget_current_position_and_unknown_report_allow_normal_refill(runtime, authority, cost_basis, cost):
    public = _FiveMarketPublic(runtime.clock)
    _, condition, token = _refill_identity(6)
    position = _position().model_copy(update={'condition_id': condition, 'token_id': token,
        'initial_value': Decimal('4') if cost_basis == 'initial-value' else None})
    store, adapter, account, lp, execution = runtime(public_client=public, positions=(position,))
    execution.lp_auto_configure({'budget_usd': '16', 'target_buy_count': 1})
    original = _seed_unknowns(store, execution, ownership=authority == 'automatic', count=1)[0]
    iid, sid = original['intent_id'], original['session_id']
    reserved = None if authority == 'manual' else '8'
    execution._auto_pool._update(lambda doc: doc['intents'][iid].update(
        reserved_usd=reserved, inventory_cost_usd=None, realized_pnl_usd='-1.40'))
    audit = deepcopy(store.lp_actions(sid))
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    if authority == 'manual':
        assert execution.lp_auto_state()['slots']['occupied'] == 1
        result = _release(execution, intent_id=iid)
        assert result['released_amount_usd'] is None
    state = execution.lp_auto_state()
    facts = execution._auto_pool._read()['account_financial_facts']
    assert facts['version'] == 2 and facts['inventory_basis'] == 'api_positions'
    assert facts['report_status'] == 'unknown' and facts['realized_pnl_usd'] is None
    assert facts['financial_status'] == state['funds']['status'] == 'known'
    assert _amount(state, 'total_usd') == 16
    assert _amount(state, 'inventory_cost_usd') == Decimal(cost)
    assert _amount(state, 'available_usd') == 16 - Decimal(cost)
    assert state['slots']['occupied'] == 0
    assert reservation_is_covered(store.lp_session(sid)) is (authority == 'automatic')
    assert bool(store.lp_session(sid).get('manual_reservation_release')) is (authority == 'manual')
    candidate = lp._read_candidate_facts({'condition_id': CONDITION_ID, 'token_id': TOKEN_ID, 'outcome': 'YES'})
    lp._candidate_pool_record_success(CONDITION_ID, {'condition_id': CONDITION_ID}, judged_at=lp._now(),
        facts={'directions': [candidate['direction']], 'account': candidate['account']})
    store.lp_save_price_history(CONDITION_ID, TOKEN_ID, [], {
        'state': 'known', 'amplitude': Decimal('.005'), 'checked_at': lp._now(),
        'valid_until': lp._now() + timedelta(days=1),
    })
    def sign(**kwargs):
        _advance(runtime)
        return SignedOrder(builder='0x1', expiration=kwargs['expiration'], maker=WALLET,
            maker_amount=8000000, metadata='0x3', order_type='GTD', salt=1,
            side='BUY', signature='0x4', signature_type=0, signer=WALLET,
            taker_amount=20000000, timestamp=1, token_id=str(kwargs['token_id']), post_only=True)
    def post(signed):
        _advance(runtime)
        account.posts.append(signed)
        account.orders = (_open_order('fixed-budget-refill', 'BUY', price='0.40', original='20'),)
        return {'order_id': 'fixed-budget-refill', 'status': 'LIVE', 'accepted': True, 'size_matched': '0'}
    account.create_limit_order, account.post_order = sign, post
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once(round_id='joint-fixed-budget-refill')
    assert len(account.posts) == 1
    _advance(runtime)
    confirmed = execution.lp_auto_run_once(round_id='joint-fixed-budget-confirm')
    assert confirmed['funds']['status'] == 'known'
    assert _amount(confirmed, 'total_usd') == 16
    assert _amount(confirmed, 'inventory_cost_usd') == Decimal(cost)
    assert _amount(confirmed, 'buy_reserved_usd') == 8
    assert _amount(confirmed, 'available_usd') == 8 - Decimal(cost)
    assert store.lp_actions(sid) == audit
    assert execution._auto_pool._read()['intents'][iid]['realized_pnl_usd'] == '-1.40'
    assert execution._auto_pool._read()['intents'][iid]['reserved_usd'] == reserved
    orders = account.orders
    adapter.close()
    _advance(runtime)
    store, adapter, account, lp, execution = runtime(public_client=public, orders=orders, positions=(position,))
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    restarted = execution.lp_auto_state()
    assert restarted['funds']['status'] == 'known'
    assert _amount(restarted, 'total_usd') == 16
    assert _amount(restarted, 'available_usd') == 8 - Decimal(cost)
    assert restarted['slots']['occupied'] == 1
    assert store.lp_actions(sid) == audit
    assert account.posts == account.cancels == account.market_orders == []


@pytest.mark.parametrize('fence', ['expired', 'generation', 'unknown'])
def test_manual_empty_account_retirement_cannot_use_invalid_current_facts(runtime, fence):
    store, adapter, account, lp, execution = runtime()
    execution.lp_auto_configure({'budget_usd': '8', 'target_buy_count': 1})
    original = _seed_unknowns(store, execution, ownership=False, count=1)[0]
    sid = original['session_id']
    store.lp_update_session(sid, patch={'buy_filled_quantity': '20', 'residual_quantity': '20', 'buy_cost': '8'})
    _release(execution, intent_id=original['intent_id'])
    _advance(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    assert not execution._auto_pool._excluded(CONDITION_ID)
    if fence == 'expired':
        _advance(runtime, 61)
    elif fence == 'generation':
        store.lp_register_trade_change(sid)
    else:
        called = Event()
        def unknown_positions(**kwargs):
            called.set()
            return None
        account.list_positions = unknown_positions
        _advance(runtime)
        execution.lp_auto_reconcile_unknown()
        assert called.is_set(), 'The real SDK position reader must supply the unknown observation'
    assert execution._auto_pool._excluded(CONDITION_ID)
    state = execution.lp_auto_state()
    assert state['funds']['spendable_usd'] is None
    if fence == 'unknown':
        # The last valid snapshot stays visible while the failed new SDK read
        # independently blocks admission; it is not replaced by invented facts.
        assert lp._account_order_sync_error in state['admission_block_reasons']
        execution.lp_auto_set_desired_running(True)
        blocked = execution.lp_auto_run_once(round_id='unknown-current-account')
        assert blocked['funds']['spendable_usd'] is None
    else:
        assert state['funds']['status'] == 'unknown'
    assert state['slots']['occupied'] == 0
    assert store.lp_session(sid)['buy_filled_quantity'] == '20'
    assert not reservation_is_covered(store.lp_session(sid))
    assert account.posts == account.cancels == []
