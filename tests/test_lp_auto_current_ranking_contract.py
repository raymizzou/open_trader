"""Current ranking contracts through native SDK feeds and public LP controls."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from threading import Event, Lock

import pytest
from timing_support import run_test_in_subprocess

from tests.test_lp_account_reservation_reconciliation import runtime, _refill_identity, _position
from tests.test_lp_auto_refill_contract import RefillPublic, prepare
from tests.test_lp_order_registration_contract import _open_order


def _run_round(engine, lp, *, round_id):
    """Run inside the process supervisor and verify local sender cleanup."""
    try:
        return engine.lp_auto_run_once(round_id=round_id)
    finally:
        lp.drain_submissions()


def test_candidate_selection_uses_current_facts_without_history_rescans(request, runtime, tmp_path, monkeypatch):
    if run_test_in_subprocess(request):
        return
    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24', 3: '96', 4: '12'}
    store, adapter, account, lp, engine, _ = prepare(runtime, count=4, target=2, public=public)
    receipts = {}

    def order_result(*, order_id):
        return receipts[order_id]

    account.get_order = order_result
    ended = []
    # Produce history with real registration, exact-ID stop and reconciliation.
    for index in range(6):
        token_index = 1 + index % 2
        oid = f'ended-{index}'
        order = _open_order(oid, 'BUY', price='.40', original='20',
            token_id=_refill_identity(token_index)[2], outcome='YES').model_copy(update={
                'market': _refill_identity(token_index)[1], 'condition_id': _refill_identity(token_index)[1]})
        receipts[oid] = order
        account.orders = (order,)
        runtime.clock[0] += timedelta(seconds=1)
        packet = adapter.lp_account_snapshot_shared(max_age_seconds=-1,
            trade_generation_provider=store.lp_trade_generation)
        assert lp.register_account_snapshot(packet)['state'] == 'registered'
        session, = [s for s in store.lp_active_sessions() if oid in s.get('owned_order_ids', ())]
        lp.stop(session['session_id'])
        receipts[oid] = order.model_copy(update={'status': 'CANCELED'})
        account.orders = ()
        runtime.clock[0] += timedelta(seconds=1)
        assert lp.reconcile_facts(session['session_id'])[3] is None
        lp.tick()
        assert store.lp_session(session['session_id'])['state'] == 'complete'
        ended.append(session['session_id'])
    history = {sid: deepcopy(store.lp_session(sid)) for sid in ended}
    # Current factual inventory on the superior third candidate excludes it.
    account.positions = (_position('10').model_copy(update={
        'token_id': _refill_identity(3)[2], 'condition_id': _refill_identity(3)[1]}),)
    runtime.clock[0] += timedelta(seconds=1)
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    lp.refresh_candidate_recommendations()
    before = engine.lp_auto_state()
    assert before['funds']['status'] == 'known' and before['slots']['occupied'] == 0
    sql = []
    native_connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            # Always execute native SQL, including the observed full reader.
            if statement == 'SELECT * FROM lp_sessions ORDER BY created_at, session_id':
                sql.append(statement)
            return super().execute(statement, parameters)

    def connect(database, *args, **kwargs):
        if Path(database).resolve() == store.path.resolve():
            kwargs['factory'] = ObservedConnection
        return native_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    planning_as_of = []
    sign = account.create_limit_order

    def observed_sign(**kwargs):
        planning_as_of.append(engine.lp_auto_state()['funds']['as_of'])
        return sign(**kwargs)

    account.create_limit_order = observed_sign
    state = _run_round(engine, lp, round_id='current-index')
    (tmp_path / 'R1-proof.json').write_text(json.dumps(dict(full_session_selects=len(sql),
        ended_ids=ended, before=before, state=state, posts=[p.token_id for p in account.posts]), default=str, indent=2))
    print(f'R1 native full-session SELECT count: {len(sql)}')
    assert Counter(p.token_id for p in account.posts) == Counter(_refill_identity(i)[2] for i in (1, 2)), state['last_round']
    assert all(p.maker_amount == 8000000 and p.taker_amount == 20000000 and p.post_only for p in account.posts)
    assert state['slots']['occupied'] == 2 and Decimal(state['funds']['buy_reserved_usd']) == 16
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == planning_as_of[0]
    assert state['funds']['spendable_usd'] is None and state['funds']['available_usd'] is None
    assert state['active_plan'] is None and state['last_round']['completed_at']
    assert {sid: store.lp_session(sid) for sid in ended} == history
    assert len(sql) <= 11, 'Candidate selection must not add per-candidate full-history SELECTs'


@pytest.mark.parametrize('outcome', ['all_accepted', 'mixed_receipts'])
def test_fixed_targets_survive_elapsed_planning_without_refill(request, runtime, tmp_path, outcome):
    if run_test_in_subprocess(request):
        return
    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '36', 3: '24', 4: '12'}
    store, adapter, account, lp, engine, _ = prepare(runtime, count=4, target=3, public=public)
    tokens = [_refill_identity(i)[2] for i in (1, 2, 3)]  # Fixed C, D, E; token4 is F.
    wire, signing, execution_reads, delayed = [], [], [], set()
    clock_lock = Lock()
    signed_event = Event()
    for name in ('get_market', 'list_market_rewards', 'get_order_book'):
        external = getattr(public, name)

        def observed(*args, _name=name, _external=external, **kwargs):
            active = engine.lp_auto_state()['active_plan'] is not None
            if active:
                execution_reads.append((_name, kwargs))
            started = runtime.clock[0]
            if _name == 'list_market_rewards':
                index = next(i for i in range(1, 5) if _refill_identity(i)[1] == kwargs['condition_id'])
                if index in (2, 3) and index not in delayed:
                    delayed.add(index)
                    runtime.clock[0] += timedelta(seconds=31)
            result = _external(*args, **kwargs)
            wire.append(dict(kind=_name, args=kwargs, started=started,
                returned=runtime.clock[0], timestamp=getattr(result, 'timestamp', None)))
            return result

        setattr(public, name, observed)
    for name in ('get_balance_allowance', 'list_open_orders', 'list_positions', 'list_account_trades'):
        external = getattr(account, name)

        def observed_account(*args, _name=name, _external=external, **kwargs):
            if engine.lp_auto_state()['active_plan'] is not None:
                execution_reads.append((_name, kwargs))
            return _external(*args, **kwargs)

        setattr(account, name, observed_account)
    sign, post = account.create_limit_order, account.post_order

    def signing_order(**kwargs):
        with clock_lock:
            approved = deepcopy(engine.lp_auto_state())
            assert approved['active_plan'], 'Signing requires the atomic fixed plan'
            signed = sign(**kwargs)
            signing.append(dict(token=kwargs['token_id'], state=approved,
                started=runtime.clock[0], expiration=signed.expiration))
            # Execution time cannot invalidate already approved market terms.
            runtime.clock[0] += timedelta(seconds=61)
            signed_event.set()
            return signed

    def venue(signed):
        if outcome == 'mixed_receipts' and signed.token_id == tokens[1]:
            account.posts.append(signed)
            raise TimeoutError('offline SDK POST response lost for fixed D')
        if outcome == 'mixed_receipts' and signed.token_id == tokens[2]:
            account.posts.append(signed)
            return {'accepted': False, 'status': 'REJECTED', 'code': 'venue_denied'}
        return post(signed)

    account.create_limit_order, account.post_order = signing_order, venue
    state = _run_round(engine, lp, round_id=f'fixed-{outcome}')
    (tmp_path / 'R2-proof.json').write_text(json.dumps(dict(wire=wire, signing=signing,
        state=state, posts=[p.token_id for p in account.posts], execution_reads=execution_reads), default=str, indent=2))
    yes_books = {row['args']['token_id']: row for row in wire
        if row['kind'] == 'get_order_book' and row['args']['token_id'] in tokens}
    assert delayed == {2, 3} and set(yes_books) == set(tokens), wire
    assert (yes_books[tokens[2]]['returned'] - yes_books[tokens[0]]['returned']).total_seconds() == 62
    assert all(row['timestamp'] == row['returned'] for row in yes_books.values())
    assert state['last_round']['candidate_filter']['counts']['recheck_removed'] == 0
    assert Counter(p.token_id for p in account.posts) == Counter(tokens), state['last_round']
    selected = {row['token_id']: row for row in state['last_round']['candidates'] if row['token_id'] in tokens}
    for token, row in selected.items():
        validated_at = datetime.fromisoformat(row['planning_entry_facts']['checked_at'])
        packet = row['planning_snapshot']
        assert validated_at == yes_books[token]['returned']
        for stamp in (packet['market']['metadata_checked_at'], packet['market']['fees_checked_at'],
                      packet['book']['received_at'], row['ranking_reward_at']):
            assert 0 <= (validated_at - datetime.fromisoformat(stamp)).total_seconds() <= 60
    assert set(selected) == set(tokens)
    assert signed_event.is_set() and Counter(row['token'] for row in signing) == Counter(tokens)
    assert execution_reads == [], 'Fixed execution performs no market/funding qualification reads'
    assert all(p.maker_amount == 8000000 and p.taker_amount == 20000000
        and p.side == 'BUY' and p.post_only and p.order_type == 'GTD' for p in account.posts)
    original = deepcopy(state['last_round'])
    assert all(row['state']['last_round']['targets'] == original['targets'] for row in signing)
    assert [t['token_id'] for t in original['targets']] == tokens
    assert all(Decimal(t['price']) == Decimal('.40') and Decimal(t['quantity']) == 20 for t in original['targets'])
    expected = dict(zip(tokens, ['success', 'success', 'success'] if outcome == 'all_accepted'
        else ['success', 'unknown', 'rejected']))
    assert {a['token_id']: a['state'] for a in original['actions']} == expected
    assert state['active_plan'] is None and original['completed_at']
    assert state['slots']['occupied'] == (3 if outcome == 'all_accepted' else 2)
    assert Decimal(state['funds']['buy_reserved_usd']) + Decimal(state['funds']['isolated_reserved_usd']) == (
        24 if outcome == 'all_accepted' else 16)
    audits = {}
    for action in original['actions']:
        audit, = store.lp_actions(action['session_id'])
        audits[action['session_id']] = deepcopy(audit)
        assert audit['token_id'] == action['token_id'] and audit['condition_id'] == action['condition_id']
        assert audit['post_started'] and Decimal(audit['price']) == Decimal('.40') and Decimal(audit['quantity']) == 20
        assert audit['state'] == {'success': 'accepted', 'unknown': 'unknown', 'rejected': 'rejected'}[action['state']]
    assert account.market_orders == account.cancels == []
    completed = datetime.fromisoformat(original['completed_at'])
    assert state['plan_wait'] is None
    reads = (account.balance_reads, account.order_reads, account.position_reads, account.trade_reads, len(wire))
    runtime.clock[0] += timedelta(seconds=1)
    quiet = engine.lp_auto_run_once(round_id=original['round_id'])
    assert quiet['last_round'] == original and len(account.posts) == 3 and len(signing) == 3
    assert account.balance_reads > reads[0] and account.order_reads > reads[1]
    assert account.trade_reads == reads[3], 'Ordinary planning does not scan history'
    assert {sid: store.lp_actions(sid) for sid in audits} == {sid: [audit] for sid, audit in audits.items()}
