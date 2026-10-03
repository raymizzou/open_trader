"""Immutable notice bodies must remain fenced by their original members."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from open_trader.notifications import FeishuAppNotifier
from tests.test_lp_notification_delivery import arm_fault, notice_service, wait_attention


def batch_case(tmp_path, automatic):
    case = SimpleNamespace(automatic=automatic, attempts=[], accepted={}, fail=True)

    def post(url, payload, headers, timeout):
        if url.endswith('tenant_access_token/internal'):
            return {'code': 0, 'tenant_access_token': 'offline-test-token'}
        case.attempts.append(deepcopy(payload))
        if case.fail:
            return {'code': 1, 'msg': 'receiver rejected request'}
        case.accepted.setdefault(payload['uuid'], json.loads(payload['content'])['text'])
        return {'code': 0}

    app = FeishuAppNotifier(app_id='offline-app', app_secret='offline-placeholder',
        receive_id_type='chat_id', receive_id='offline-chat', post_json=post)
    if automatic:
        from tests.test_lp_auto_pool import setup
        case.engine, _, case.service, case.store = setup(tmp_path, 2)
        case.current = [case.service._now()]
        case.service.clock = lambda: case.current[0]
        case.engine._notifier = app
        case.engine.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 2})
        case.engine.lp_auto_set_desired_running(True)
        case.rows = case.engine.lp_auto_run_once()['intents']
        wait_attention(case.service)
        case.pool = case.engine._auto_pool
        for row, title in zip(case.rows, ['Market Alpha', 'Market Beta']):
            case.store.lp_update_session(row['session_id'], patch={'market_title': title})
            case.pool._update(lambda d, row=row: d['intents'][row['intent_id']].update(
                attention_episode=row['intent_id'] + ':fault',
                attention_since=case.current[0].isoformat(),
                attention_recovery_due=True, attention_delivered_channels=['feishu_app'],
                attention_recovery_ready_since=(case.current[0]-timedelta(seconds=60)).isoformat(),
                attention_recovery_first_checked_at=(case.current[0]-timedelta(seconds=60)).isoformat(),
                checked_at=case.current[0].isoformat(), financial_status='known',
                reconcile_error=None, reconcile_reason=None))
        case.flush = case.pool.flush_attention
        case.read = lambda index: case.pool._read()['intents'][case.rows[index]['intent_id']]
        case.due_key = 'attention_recovery_due'
    else:
        case.service, case.store, case.current, _ = notice_service(tmp_path)
        case.rows = [{'session_id': sid} for sid in ['one', 'two']]
        for row, title in zip(case.rows, ['Market Alpha', 'Market Beta']):
            sid = row['session_id']
            arm_fault(case.store, sid, title)
            case.store.lp_update_session(sid, state='entry_open', patch={
                'needs_attention_due': False, 'needs_attention_recovery_due': True,
                'needs_attention_recovery_episode': sid+':fault',
                'needs_attention_recovery_channels': ['feishu'],
                'needs_attention_recovery_ready_since': (case.current[0]-timedelta(seconds=60)).isoformat(),
                'needs_attention_recovery_first_checked_at': (case.current[0]-timedelta(seconds=60)).isoformat(),
                'position_reconciled': True, 'facts_checked_at': case.current[0].isoformat()})

        def notify(title, message, voice, *, channels):
            results = {}
            for channel in channels:
                try:
                    app.notify(title, message)
                except Exception:
                    results[channel] = False
                else:
                    results[channel] = True
            return results

        case.service.set_protection_notifier(notify)
        case.flush = lambda: [case.service.flush_session_recovery(row['session_id']) for row in case.rows]
        case.read = lambda index: case.store.lp_session(case.rows[index]['session_id'])
        case.due_key = 'needs_attention_recovery_due'

    def advance():
        case.current[0] += timedelta(seconds=61)
        for row in case.rows:
            if automatic:
                case.pool._update(lambda d, row=row: d['intents'][row['intent_id']].update(
                    checked_at=case.current[0].isoformat()))
            else:
                case.store.lp_update_session(row['session_id'], patch={'facts_checked_at': case.current[0].isoformat()})

    case.advance = advance
    return case


def business_image(case):
    sessions = deepcopy(case.store.lp_sessions())
    for row in sessions:
        for key in list(row):
            if key.startswith(('attention_', 'needs_attention_', '_lp_revision')) or key == 'updated_at':
                row.pop(key)
    if not case.automatic:
        return sessions
    document = deepcopy(case.pool._read())
    for row in document['intents'].values():
        for key in list(row):
            if key.startswith('attention_'):
                row.pop(key)
    return sessions, document, case.pool.state(include_intents=False)


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('invalidate', ['archive', 'new_fault'])
def test_retired_member_gets_no_stale_text_or_ack_from_surviving_batch(tmp_path, automatic, invalidate):
    case = batch_case(tmp_path, automatic)
    case.flush()
    assert len(case.attempts) == 1
    old_id = case.attempts[0]['uuid']
    old_body = json.loads(case.attempts[0]['content'])['text']
    assert 'Market Alpha' in old_body and 'Market Beta' in old_body
    case.advance()
    second = case.rows[1]
    sid = second['session_id']
    if invalidate == 'archive':
        patch = {'account_baseline_archive': {'id': 'archived-baseline'}}
        case.store.lp_update_session(sid, patch=patch)
        if automatic:
            case.pool._update(lambda d: d['intents'][second['intent_id']].update(patch))
    elif automatic:
        case.pool._update(lambda d: d['intents'][second['intent_id']].update(
            attention_episode='new-fault', attention_due=False, attention_recovery_due=False,
            financial_status='unknown', reconcile_error='new-failure', reconcile_reason='new-failure'))
    else:
        case.store.lp_update_session(sid, state='needs_attention', patch={
            'needs_attention_episode': 'new-fault', 'needs_attention_due': False,
            'needs_attention_recovery_episode': None, 'needs_attention_recovery_due': False,
            'reconciliation': 'new-failure'})
    invalidated = deepcopy(case.read(1))
    before = business_image(case)
    # The new survivor event also fails once. Its next retry must be stable.
    case.flush()
    assert len(case.attempts) == 2
    replacement = case.attempts[-1]
    assert replacement['uuid'] != old_id
    replacement_body = json.loads(replacement['content'])['text']
    assert 'Market Alpha' in replacement_body and 'Market Beta' not in replacement_body
    assert case.read(1) == invalidated
    assert business_image(case) == before
    case.advance()
    case.fail = False
    before = business_image(case)
    case.flush()
    assert len(case.attempts) == 3
    assert case.attempts[-1]['uuid'] == replacement['uuid']
    assert case.attempts[-1]['content'] == replacement['content']
    assert not case.read(0).get(case.due_key)
    assert business_image(case) == before


@pytest.mark.parametrize('automatic', [False, True])
def test_frozen_batch_waits_when_original_pending_member_loses_recovery_facts(tmp_path, automatic):
    case = batch_case(tmp_path, automatic)
    case.flush()
    assert len(case.attempts) == 1
    case.advance()
    first = case.rows[0]
    if automatic:
        case.pool._update(lambda d: d['intents'][first['intent_id']].update(
            financial_status='unknown', reconcile_error='trade_change_pending', reconcile_reason='trade_change_pending'))
    else:
        case.store.lp_update_session(first['session_id'], patch={'facts_error': 'trade_change_pending'})
    case.fail = False
    before = business_image(case)
    case.flush()
    assert len(case.attempts) == 1
    assert all(case.read(index).get(case.due_key) for index in range(2))
    assert business_image(case) == before


@pytest.mark.parametrize('automatic', [False, True])
def test_late_old_batch_success_cannot_ack_failed_replacement(tmp_path, monkeypatch, automatic, request):
    from timing_support import run_test_in_subprocess
    if run_test_in_subprocess(request):
        return
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    from tests.test_lp_notification_batches import make_batch_case

    case = make_batch_case(tmp_path, automatic)
    entered, release, counter_lock = Event(), Event(), Lock()
    post = case.app._post_json
    requests = [0]

    def delayed_first(url, payload, headers, timeout):
        if url.endswith('/auth/v3/tenant_access_token/internal'):
            return post(url, payload, headers, timeout)
        with counter_lock:
            index = requests[0]
            requests[0] += 1
        if index == 0:
            case.calls.append(deepcopy(payload))
            entered.set()
            assert release.wait(5), 'old notification transport was not released'
            case.accepted.setdefault(payload['uuid'], json.loads(payload['content'])['text'])
            return {'code': 0}
        return post(url, payload, headers, timeout)

    monkeypatch.setattr(case.app, '_post_json', delayed_first)
    with ThreadPoolExecutor(max_workers=1) as workers:
        old = workers.submit(case.flush, 0)
        try:
            assert entered.wait(5), 'old notification did not start'
            case.refresh(0, 1, advance=61)
            case.store.lp_update_session(case.sids[1], patch={'account_baseline_archive': {'id': 'archive'}})
            if automatic:
                case.patch(1, account_baseline_archive={'id': 'archive'})
            archived = deepcopy(case.read(1))
            case.restart()  # An independent delivery owner can retire the old batch.
            before = case.business_image()
            case.flush(0)  # Replacement transport rejects; it remains pending.
            assert len(case.calls) == 2
            assert case.calls[0]['uuid'] != case.calls[1]['uuid']
            assert case.read(0).get(case.prefix + '_due')
        finally:
            release.set()
        old.result(timeout=5)
    assert case.read(0).get(case.prefix + '_due')
    assert case.read(1) == archived
    assert case.business_image() == before
    if automatic:
        assert case.channel not in case.read(0).get('attention_recovery_delivered_channels', [])
    else:
        assert not case.read(0).get('needs_attention_recovery_channel_status', {}).get(case.channel)


def test_auto_claim_rechecks_nonterminal_recovery_evidence(tmp_path, monkeypatch):
    case = batch_case(tmp_path, True)
    update = case.pool._update
    changed = []

    def invalidate_before_claim(fn, **kwargs):
        if fn.__name__ == 'claim' and not changed:
            changed.append(True)
            for row in case.rows:
                update(lambda d, row=row: d['intents'][row['intent_id']].update(
                    checked_at=(case.current[0] - timedelta(seconds=61)).isoformat()))
        return update(fn, **kwargs)

    monkeypatch.setattr(case.pool, '_update', invalidate_before_claim)
    case.flush()
    assert changed == [True]
    assert case.attempts == []
    assert all(case.read(index).get(case.due_key) for index in range(2))


def test_manual_finalization_rejects_generation_change_after_first_claim(tmp_path, monkeypatch):
    case = batch_case(tmp_path, False)
    claim = case.store.lp_claim_attention_notification
    changed = []

    def invalidate_after_claim(sid, **kwargs):
        result = claim(sid, **kwargs)
        if result is not None and not changed:
            changed.append(True)
            case.store.lp_register_trade_change(sid)
        return result

    monkeypatch.setattr(case.store, 'lp_claim_attention_notification', invalidate_after_claim)
    case.service.flush_session_recovery(case.rows[0]['session_id'])
    assert changed == [True]
    assert case.attempts == []
    assert all(case.read(index).get(case.due_key) for index in range(2))


@pytest.mark.parametrize('change', ['revision', 'archive', 'episode', 'generation'])
def test_manual_finalization_fences_acknowledged_original_member(tmp_path, monkeypatch, change):
    from tests.test_lp_notification_batches import make_batch_case

    case = make_batch_case(tmp_path, False)
    case.flush()
    case.refresh(0, 1, advance=60)
    case.defer(1, 60)
    case.fail_feishu = False
    case.flush(0)
    assert len(case.calls) == 2
    assert not case.read(0).get(case.prefix + '_due')
    assert case.read(1).get(case.prefix + '_due')
    case.refresh(1, advance=61)
    finalize = case.store.lp_finalize_attention_notification_batch
    mutated = []
    before = []

    def mutate_before_finalize(**kwargs):
        assert case.sids[0] in kwargs['guards']
        if not mutated:
            mutated.append(True)
            if change == 'revision':
                case.store.lp_update_session(case.sids[0], patch={'scoring_status': 'changed'})
            elif change == 'archive':
                case.store.lp_update_session(case.sids[0], patch={'account_baseline_archive': {'id': 'archive'}})
            elif change == 'episode':
                case.store.lp_update_session(case.sids[0], state='needs_attention', patch={
                    'needs_attention_episode': 'new-fault', 'needs_attention_recovery_episode': None,
                    'needs_attention_recovery_due': False})
            else:
                case.store.lp_create_session('unrelated', 'unrelated', state='entry_open', payload={})
                case.store.lp_register_trade_change('unrelated')
            before.append(case.business_image())
        return finalize(**kwargs)

    monkeypatch.setattr(case.store, 'lp_finalize_attention_notification_batch', mutate_before_finalize)
    case.flush(1)
    assert mutated == [True]
    assert len(case.calls) == 2
    assert case.read(1).get(case.prefix + '_due')
    assert case.business_image() == before[0]
