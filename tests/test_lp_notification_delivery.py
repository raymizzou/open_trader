from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def notice_service(tmp_path):
    current = [NOW]
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, object(), clock=lambda: current[0])
    calls = []
    service.set_protection_notifier(
        lambda title, message, voice, *, channels: calls.append(
            (title, message, channels)
        ) or {channel: True for channel in channels}
    )
    return service, store, current, calls


def arm_fault(store, sid, title, *, reason="external_snapshot_unknown"):
    return store.lp_create_session(sid, sid, state="needs_attention", payload={
        "condition_id": sid,
        "token_id": sid,
        "market_title": title,
        "reconciliation": reason,
        "needs_attention_since": (NOW - timedelta(seconds=301)).isoformat(),
        "needs_attention_episode": sid + ":fault",
        "needs_attention_due": True,
        "needs_attention_channel_status": {},
    })


def test_delivery_ack_failure_retries_ack_without_resending(tmp_path, monkeypatch):
    service, store, current, calls = notice_service(tmp_path)
    arm_fault(store, "one", "市场一")
    finish = store.lp_finish_attention_notification
    attempts = []

    def busy_once(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return finish(*args, **kwargs)

    monkeypatch.setattr(store, "lp_finish_attention_notification", busy_once)
    with pytest.raises(sqlite3.OperationalError):
        service.flush_session_attention("one")
    assert len(calls) == 1
    current[0] += timedelta(seconds=61)
    service.flush_session_attention("one")
    assert len(calls) == 1
    assert store.lp_session("one")["needs_attention_notified"] is True


def test_same_reason_notices_are_batched_with_market_and_pool_context(tmp_path):
    service, store, _, calls = notice_service(tmp_path)
    arm_fault(store, "one", "市场一")
    arm_fault(store, "two", "市场二")
    store.lp_update_session("one", patch={"queue_protection": {"data_failures": 3}})
    service._facts_attention_summary = lambda: {
        "desired_running": True, "target_buy_count": 5,
        "slots": {"active": 0, "pending_review": 5},
        "funds": {"pending_reserved_usd": "83.260"},
        "reason": "submission_unknown",
    }
    service.flush_session_attention("one")
    service.flush_session_attention("two")
    assert len(calls) == 1
    assert "市场一" in calls[0][1] and "市场二" in calls[0][1]
    assert "0/5" in calls[0][1] and "83.260" in calls[0][1]
    assert "受阻" in calls[0][1]
    assert "数据读取失败 3/10，满 10 次将保护性撤单" in calls[0][1]
    assert all(store.lp_session(sid)["needs_attention_notified"] for sid in ("one", "two"))


@pytest.mark.parametrize("reason", ["market_read_capacity", "market_read_in_progress", "execution_lock"])
def test_internal_wait_does_not_send_external_failure_alert(tmp_path, reason):
    service, store, _, calls = notice_service(tmp_path)
    arm_fault(store, "one", "市场一", reason=reason)
    service.flush_session_attention("one")
    assert calls == []
    assert store.lp_session("one")["state"] == "needs_attention"


@pytest.mark.parametrize("reason", [
    "market_read_capacity", "market_read_in_progress", "execution_lock",
    "facts_read_capacity", "facts_read_in_progress", "account_round_invalid",
])
def test_current_internal_wait_overrides_historical_fault_copy(tmp_path, reason):
    service, store, _, calls = notice_service(tmp_path)
    arm_fault(store, "one", "市场一")
    service._publish_facts_wait("one", reason)
    service.flush_session_attention("one")
    assert calls == []
    assert store.lp_session("one")["reconciliation"] == "external_snapshot_unknown"
    assert store.lp_session("one")["facts_error"] == reason


@pytest.mark.parametrize("change", ["archive", "episode", "facts", "trade"])
@pytest.mark.parametrize("recovery", [False, True])
def test_manual_claim_rechecks_the_session_image(tmp_path, monkeypatch, change, recovery):
    service, store, current, calls = notice_service(tmp_path)
    arm_fault(store, "one", "市场一")
    if recovery:
        store.lp_update_session("one", state="entry_open", patch={
            "needs_attention_due": False, "needs_attention_recovery_due": True,
            "needs_attention_recovery_episode": "one:fault",
            "needs_attention_recovery_channels": ["feishu"],
            "position_reconciled": True, "facts_checked_at": current[0].isoformat(),
        })
        service.flush_session_recovery("one")
        current[0] += timedelta(seconds=60)
        store.lp_update_session("one", patch={"facts_checked_at": current[0].isoformat()})
    claim = store.lp_claim_attention_notification
    raced = []

    def invalidate(sid, **kwargs):
        patch = {"facts_error": "account_facts_incomplete"}
        if change == "archive":
            patch = {"account_baseline_archive": {"manifest_id": "archive"}}
        elif change == "episode":
            key = "needs_attention_recovery_episode" if recovery else "needs_attention_episode"
            patch = {key: "new-episode"}
        if change == "trade":
            store.lp_register_trade_change(sid)
        else:
            store.lp_update_session(sid, patch=patch)
        raced.append(True)
        return claim(sid, **kwargs)

    monkeypatch.setattr(store, "lp_claim_attention_notification", invalidate)
    flush = service.flush_session_recovery if recovery else service.flush_session_attention
    flush("one")
    assert raced == [True]
    assert calls == []
    assert not store.lp_session("one").get("needs_attention_sending")


def test_recovery_waits_for_fresh_verified_facts_and_keeps_scope(tmp_path):
    service, store, current, calls = notice_service(tmp_path)
    arm_fault(store, "one", "市场一")
    service.flush_session_attention("one")
    store.lp_update_session("one", state="entry_open", patch={
        "reconciliation": None,
        "needs_attention_recovery_due": True,
        "needs_attention_recovery_episode": "one:fault",
        "needs_attention_recovery_channels": ["feishu"],
        "position_reconciled": True,
        "facts_checked_at": NOW.isoformat(),
    })
    service.flush_session_recovery("one")
    assert len(calls) == 1
    current[0] += timedelta(seconds=60)
    service.flush_session_recovery("one")
    assert len(calls) == 1  # Replaying the same observation is not recovery proof.
    store.lp_update_session("one", patch={"facts_checked_at": current[0].isoformat()})
    service.flush_session_recovery("one")
    assert len(calls) == 2
    assert "市场一" in calls[-1][1]
    assert "会话" in calls[-1][0]


def test_auto_delivery_ack_failure_does_not_repeat_successful_channel(tmp_path, monkeypatch):
    from tests.test_lp_auto_pool import setup

    engine, _, lp, _ = setup(tmp_path)
    current = [lp._now()]
    lp.clock = lambda: current[0]
    calls = []

    class Notifier:
        def notify(self, title, message):
            calls.append((title, message))

    engine._notifier = Notifier()
    engine.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1})
    engine.lp_auto_set_desired_running(True)
    intent_id = engine.lp_auto_run_once()["intents"][0]["intent_id"]
    pool = engine._auto_pool
    pool._update(lambda d: d["intents"][intent_id].update(
        attention_due=True, attention_episode="fault", attention_since=current[0].isoformat(),
        reconcile_error="external_snapshot_unknown", financial_status="unknown"))
    update = pool._update
    busy = [True]

    def fail_ack(fn, **kwargs):
        if fn.__name__ == "apply" and busy:
            busy.pop()
            raise sqlite3.OperationalError("database is locked")
        return update(fn, **kwargs)

    monkeypatch.setattr(pool, "_update", fail_ack)
    with pytest.raises(sqlite3.OperationalError):
        pool._deliver_attention(intent_id)
    assert len(calls) == 1
    current[0] += timedelta(seconds=61)
    pool._deliver_attention(intent_id)
    assert len(calls) == 1
    assert pool._read()["intents"][intent_id]["attention_notified"] is True


def test_archive_between_auto_read_and_claim_prevents_delivery(tmp_path, monkeypatch):
    from tests.test_lp_auto_pool import setup

    engine, _, _, _ = setup(tmp_path)
    calls = []

    class Notifier:
        def notify(self, *args):
            calls.append(args)

    engine._notifier = Notifier()
    engine.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1})
    engine.lp_auto_set_desired_running(True)
    intent_id = engine.lp_auto_run_once()["intents"][0]["intent_id"]
    pool = engine._auto_pool
    pool._update(lambda d: d["intents"][intent_id].update(
        attention_due=True, attention_episode="fault", reconcile_error="external_snapshot_unknown"))
    update = pool._update

    def archive_before_claim(fn, **kwargs):
        if fn.__name__ == "claim":
            update(lambda d: d["intents"][intent_id].update(account_baseline_archive={"id": "baseline"}))
        return update(fn, **kwargs)

    monkeypatch.setattr(pool, "_update", archive_before_claim)
    pool._deliver_attention(intent_id)
    assert not calls


def test_auto_claim_rejects_changed_reason_in_same_episode(tmp_path, monkeypatch):
    from tests.test_lp_auto_pool import setup

    engine, _, lp, _ = setup(tmp_path)
    calls = []

    class Notifier:
        def notify(self, *args):
            calls.append(args)

    engine._notifier = Notifier()
    engine.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1})
    engine.lp_auto_set_desired_running(True)
    iid = engine.lp_auto_run_once()["intents"][0]["intent_id"]
    pool = engine._auto_pool
    update = pool._update
    update(lambda d: d["intents"][iid].update(
        attention_due=True, attention_episode="fault", attention_since=lp._now().isoformat(),
        reconcile_error="external_snapshot_unknown", financial_status="unknown"))
    raced = []

    def change_reason(fn, **kwargs):
        if fn.__name__ == "claim":
            update(lambda d: d["intents"][iid].update(reconcile_error="position_mismatch"))
            raced.append(True)
        return update(fn, **kwargs)

    monkeypatch.setattr(pool, "_update", change_reason)
    pool._deliver_attention(iid)
    assert raced == [True]
    assert calls == []
    current = pool._read()["intents"][iid]
    assert current["attention_due"] is True
    assert not current.get("attention_notified")


def test_cached_fault_ack_survives_recovery_before_ack_retry(tmp_path, monkeypatch):
    from tests.test_lp_auto_pool import setup

    engine, _, lp, _ = setup(tmp_path)
    current = [lp._now()]
    lp.clock = lambda: current[0]
    calls = []

    class Notifier:
        def notify(self, title, message):
            calls.append((title, message))

    engine._notifier = Notifier()
    engine.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1})
    engine.lp_auto_set_desired_running(True)
    row = engine.lp_auto_run_once()["intents"][0]
    iid, sid = row["intent_id"], row["session_id"]
    pool = engine._auto_pool
    pool._update(lambda d: d["intents"][iid].update(
        attention_due=True, attention_episode="fault", attention_since=current[0].isoformat(),
        reconcile_error="external_snapshot_unknown", financial_status="unknown"))
    update = pool._update
    busy = [True]

    def fail_ack(fn, **kwargs):
        if fn.__name__ == "apply" and busy:
            busy.pop()
            raise sqlite3.OperationalError("database is locked")
        return update(fn, **kwargs)

    monkeypatch.setattr(pool, "_update", fail_ack)
    with pytest.raises(sqlite3.OperationalError):
        pool._deliver_attention(iid)
    current[0] += timedelta(seconds=61)
    update(lambda d: d["intents"][iid].update(
        financial_status="known", reconcile_reason=None, reconcile_error=None,
        attention_due=False, attention_recovery_due=False,
        attention_recovered_at=current[0].isoformat(), checked_at=current[0].isoformat()))
    pool.flush_attention(sid)
    assert len(calls) == 1
    assert pool._read()["intents"][iid]["attention_recovery_due"] is True
    current[0] += timedelta(seconds=60)
    update(lambda d: d["intents"][iid].update(checked_at=current[0].isoformat()))
    pool.flush_attention(sid)
    assert len(calls) == 2
    assert calls[-1][0].startswith("LP 标的资金核对恢复")
    assert not pool._read()["intents"][iid].get("attention_since")


def wait_attention(service):
    thread = service._attention_thread
    if thread is not None:
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize('automatic', [False, True])
def test_recovery_observation_interval_uses_facts_not_flush_time(tmp_path, automatic):
    if automatic:
        from tests.test_lp_auto_pool import setup
        engine, _, service, store = setup(tmp_path)
        current = [service._now()]
        service.clock = lambda: current[0]
        calls = []
        class Notifier:
            def notify(self, title, message):
                calls.append((title, message))
        engine._notifier = Notifier()
        engine.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 1})
        engine.lp_auto_set_desired_running(True)
        row = engine.lp_auto_run_once()['intents'][0]
        pool, iid = engine._auto_pool, row['intent_id']
        pool._update(lambda d: d['intents'][iid].update(
            attention_episode='fault', attention_since=current[0].isoformat(),
            attention_recovery_due=True, attention_delivered_channels=['Notifier'],
            financial_status='known', reconcile_error=None, reconcile_reason=None,
            checked_at=current[0].isoformat()))
        def stamp(value):
            pool._update(lambda d: d['intents'][iid].update(checked_at=value.isoformat()))
        flush = lambda: pool.flush_attention(row['session_id'])
        due = lambda: pool._read()['intents'][iid].get('attention_recovery_due')
    else:
        service, store, current, calls = notice_service(tmp_path)
        arm_fault(store, 'one', '市场一')
        store.lp_update_session('one', state='entry_open', patch={
            'needs_attention_due': False, 'needs_attention_recovery_due': True,
            'needs_attention_recovery_episode': 'one:fault',
            'needs_attention_recovery_channels': ['feishu'],
            'position_reconciled': True, 'facts_checked_at': current[0].isoformat()})
        stamp = lambda value: store.lp_update_session('one', patch={'facts_checked_at': value.isoformat()})
        flush = lambda: service.flush_session_recovery('one')
        due = lambda: store.lp_session('one')['needs_attention_recovery_due']
    first = current[0]
    flush()
    current[0] = first + timedelta(seconds=60)
    flush()
    assert not calls and due()  # Same stamp replay.
    stamp(first + timedelta(seconds=1))
    flush()
    assert not calls and due()  # Fresh age=59, but observations only 1s apart.
    stamp(first + timedelta(seconds=59))
    flush()
    assert not calls and due()
    stamp(first + timedelta(seconds=60))
    flush()
    assert len(calls) == 1 and not due()  # Exact observation boundary.


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('ack_failure', [False, True])
def test_heterogeneous_recovery_channels_keep_payload_and_retry_scoped(tmp_path, monkeypatch, automatic, ack_failure):
    from open_trader.notifications import CompositeNotifier, FeishuWebhookNotifier, XiaoaiSSHNotifier
    calls, fail = [], [True]
    if automatic:
        from tests.test_lp_auto_pool import setup
        engine, _, service, store = setup(tmp_path, 2)
        current = [service._now()]
        service.clock = lambda: current[0]
        class Feishu(FeishuWebhookNotifier):
            def __init__(self): pass
            def notify(self, title, message): calls.append(('feishu', message))
        class Xiaoai(XiaoaiSSHNotifier):
            def __init__(self): pass
            def notify(self, title, message):
                calls.append(('xiaoai', message))
                if fail[0]: raise RuntimeError('voice unavailable')
        engine._notifier = CompositeNotifier([Feishu(), Xiaoai()])
        engine.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 2})
        engine.lp_auto_set_desired_running(True)
        rows = engine.lp_auto_run_once()['intents']
        pool = engine._auto_pool
        for row, channel, title in zip(rows, ('feishu', 'xiaoai'), ('市场甲', '市场乙')):
            store.lp_update_session(row['session_id'], patch={'market_title': title})
            pool._update(lambda d, row=row, channel=channel: d['intents'][row['intent_id']].update(
                attention_episode=row['intent_id'] + ':fault', attention_since=current[0].isoformat(),
                attention_recovery_due=True, attention_delivered_channels=[channel],
                attention_recovery_ready_since=(current[0]-timedelta(seconds=60)).isoformat(),
                attention_recovery_first_checked_at=(current[0]-timedelta(seconds=60)).isoformat(),
                checked_at=current[0].isoformat(), financial_status='known', reconcile_error=None, reconcile_reason=None))
        flush = pool.flush_attention
        due = lambda index: pool._read()['intents'][rows[index]['intent_id']].get('attention_recovery_due', False)
        group = lambda: pool._read()['intents'][rows[1]['intent_id']]['attention_recovery_delivery_group']
    else:
        service, store, current, _ = notice_service(tmp_path)
        for sid, channel, title in zip(('one', 'two'), ('feishu', 'xiaoai'), ('市场甲', '市场乙')):
            arm_fault(store, sid, title)
            store.lp_update_session(sid, state='entry_open', patch={
                'needs_attention_due': False, 'needs_attention_recovery_due': True,
                'needs_attention_recovery_episode': sid+':fault',
                'needs_attention_recovery_channels': [channel],
                'needs_attention_recovery_ready_since': (current[0]-timedelta(seconds=60)).isoformat(),
                'needs_attention_recovery_first_checked_at': (current[0]-timedelta(seconds=60)).isoformat(),
                'position_reconciled': True, 'facts_checked_at': current[0].isoformat()})
        def notify(title, message, voice, *, channels):
            for channel in sorted(channels): calls.append((channel, message))
            return {channel: channel != 'xiaoai' or not fail[0] for channel in channels}
        service.set_protection_notifier(notify)
        flush = lambda: service.flush_session_recovery('two')
        due = lambda index: store.lp_session(('one', 'two')[index])['needs_attention_recovery_due']
        group = lambda: store.lp_session('two')['needs_attention_recovery_delivery_group']
    if ack_failure:
        cache = pool._attention_delivery_results if automatic else service._attention_delivery_results
        finish = pool._finish_attention_delivery if automatic else store.lp_finish_attention_notification
        busy = [True]
        def finish_once(*args, **kwargs):
            if busy:
                busy.pop()
                assert len(cache) == 2  # All channel results cached before the first SQLite ACK.
                if automatic:
                    assert sorted(sorted(value[1]) for value in cache.values()) == [[], ['feishu']]
                else:
                    assert sorted(sorted(value.items()) for value in cache.values()) == [[('feishu', True)], [('xiaoai', False)]]
                raise sqlite3.OperationalError('database is locked')
            return finish(*args, **kwargs)
        monkeypatch.setattr(pool if automatic else store,
            '_finish_attention_delivery' if automatic else 'lp_finish_attention_notification', finish_once)
        with pytest.raises(sqlite3.OperationalError):
            flush()
        current[0] += timedelta(seconds=61)
        if not automatic:
            service.flush_session_recovery('one')
        flush()  # ACK retries use cached results; expired facts forbid another send.
        assert len(calls) == 2
    else:
        flush()
    assert not due(0) and due(1)  # A closes; B alone owes voice recovery.
    assert sorted(channel for channel, _ in calls) == ['feishu', 'xiaoai']
    for channel, message in calls:
        expected, excluded = ('市场甲', '市场乙') if channel == 'feishu' else ('市场乙', '市场甲')
        assert expected in message and excluded not in message
    delivery_group = group()
    fail[0] = False
    current[0] += timedelta(seconds=60)
    if automatic:
        pool._update(lambda d: d['intents'][rows[1]['intent_id']].update(checked_at=current[0].isoformat()))
    else:
        store.lp_update_session('two', patch={'facts_checked_at': current[0].isoformat()})
    assert group() == delivery_group
    flush()
    assert not due(0) and not due(1)
    assert [channel for channel, _ in calls].count('feishu') == 1
    assert calls[-1][0] == 'xiaoai' and '市场乙' in calls[-1][1] and '市场甲' not in calls[-1][1]
    flush()
    assert len(calls) == 3


@pytest.mark.parametrize('terminal', ['complete', 'entry_rejected'])
@pytest.mark.parametrize('route', ['monitor', 'explicit', 'reuse', 'reports', 'manual'])
@pytest.mark.parametrize('second', ['fresh', 'replay', 'lagged', 'failure', 'position', 'unresolved', 'generation', 'lock', 'archive'])
def test_terminal_recovery_verification_is_read_only(tmp_path, monkeypatch, route, second, terminal):
    from copy import deepcopy
    from tests import test_lp_auto_pool as venue
    from tests.test_lp_auto_pool import setup, _manual_request

    engine, exchange, service, store = setup(tmp_path)
    first = venue.NOW
    current = [first]
    service.clock = lambda: current[0]
    monkeypatch.setattr(venue, 'NOW', first)
    calls, reads = [], []
    class Notifier:
        def notify(self, title, message): calls.append((title, message))
    engine._notifier = Notifier()
    if route == 'manual':
        engine.lp_auto_state()  # Bind the existing shared facts publisher.
        sid = 'manual-terminal'
        exchange.orders = [dict(order_id='o1', token_id='m00', condition_id='m00',
                                side='BUY', status='LIVE', price='.4', original_size='10', size_matched='0')]
        store.lp_create_session(sid, sid, state='entry_open', payload={
            **_manual_request(first), 'entry_order_id': 'o1', 'owned_order_ids': ['o1'], 'submit_status': 'accepted'})
        service.set_protection_notifier(lambda title, message, voice, **kwargs: calls.append((title, message)) or True)
        iid = None
    else:
        engine.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 1})
        engine.lp_auto_set_desired_running(True)
        row = engine.lp_auto_run_once()['intents'][0]
        iid, sid = row['intent_id'], row['session_id']
    original = exchange.lp_snapshot
    fail = [True]
    def snapshot(request):
        reads.append(current[0])
        if fail[0]: raise OSError('account unavailable')
        result = deepcopy(original(request))
        result['account']['checked_at'] = current[0]
        if current[0] > first + timedelta(seconds=301):
            if second in ('replay', 'lagged'):
                result['account']['checked_at'] = first + timedelta(seconds=301 + (second == 'lagged'))
            elif second == 'generation':
                result['_lp_trade_generation'] = store.lp_trade_generation() + 1
        return result
    exchange.lp_snapshot = snapshot
    engine.lp_tick() if route == 'manual' else engine.lp_auto_reconcile_unknown()
    current[0] = first + timedelta(seconds=300)
    engine.lp_tick() if route == 'manual' else engine.lp_auto_reconcile_unknown()
    wait_attention(service)
    assert len(calls) == 1
    current[0] = first + timedelta(seconds=301)
    fail[0] = False
    exchange.orders[0]['status'] = 'CANCELED'
    engine.lp_tick()
    wait_attention(service)
    assert store.lp_session(sid)['state'] == 'complete'
    if terminal == 'entry_rejected':
        # A late rejection receipt uses this durable state writer; the existing
        # ledger projection retains previously settled facts and pending recovery.
        exchange.orders[0]['status'] = 'REJECTED'
        rejected = store.lp_update_session(sid, state='entry_rejected', patch={'submit_status': 'rejected'})
        if iid:
            engine._auto_pool._record_session(iid, rejected)
        wait_attention(service)
    assert store.lp_session(sid)['state'] == terminal
    if iid:
        before = engine._auto_pool._read()['intents'][iid]
        assert before['settled'] is True and before['report_pending'] is False
        assert before['attention_recovery_due'] is True
        assert before['attention_recovery_first_checked_at'] == current[0].isoformat()
    else:
        assert store.lp_session(sid)['needs_attention_recovery_due'] is True
        assert store.lp_session(sid)['needs_attention_recovery_first_checked_at'] == current[0].isoformat()
    generation, posts, actions = store.lp_trade_generation(), deepcopy(exchange.posts), store.lp_actions(sid)
    business_keys = ('state', 'position_reconciled', 'orders_terminal', 'buy_cost', 'residual_quantity', 'facts_checked_at')
    business = {key: store.lp_session(sid).get(key) for key in business_keys}
    projection = engine.lp_auto_state()
    prior_reads = len(reads)
    current[0] += timedelta(seconds=59)
    def run():
        if route in ('monitor', 'manual'): engine.lp_tick()
        elif route == 'explicit': engine.lp_auto_reconcile_unknown()
        elif route == 'reuse': engine._auto_pool._reconcile_unknown(reuse=True, bounded=True)
        else: engine._auto_pool.reconcile_reports()
        wait_attention(service)
    run()
    assert len(reads) == prior_reads and len(calls) == 1
    current[0] += timedelta(seconds=1)
    if second == 'failure': fail[0] = True
    elif second == 'position': exchange.positions = [{'token_id': 'm00', 'size': '1'}]
    elif second == 'unresolved': exchange.orders[0]['status'] = 'UNKNOWN'
    elif second == 'lock':
        monkeypatch.setattr(engine, '_acquire_global_lock', lambda: None)
    elif second == 'archive':
        store.lp_update_session(sid, patch={'account_baseline_archive': {'id': 'archived'}})
        if iid:
            engine._auto_pool._update(lambda d: d['intents'][iid].update(account_baseline_archive={'id': 'archived'}))
    store.lp_update_session(sid, patch={'report_checked_at': current[0].isoformat()})
    run()
    assert len(reads) == prior_reads + (second != 'archive')
    assert len(calls) == (2 if second == 'fresh' else 1)
    assert {key: store.lp_session(sid).get(key) for key in business_keys} == business
    assert store.lp_trade_generation() == generation and exchange.posts == posts and store.lp_actions(sid) == actions
    after_projection = engine.lp_auto_state()
    assert {k: v for k, v in after_projection['funds'].items() if k != 'as_of'} == {k: v for k, v in projection['funds'].items() if k != 'as_of'}
    assert after_projection['slots'] == projection['slots']  # Scheduler check time may advance; money and admission do not.
    if iid:
        after = engine._auto_pool._read()['intents'][iid]
        for key in ('settled', 'report_pending', 'state', 'reserved_usd', 'inventory_cost_usd', 'realized_pnl_usd', 'financial_status'):
            assert after.get(key) == before.get(key)
        assert bool(after.get('attention_recovery_due')) == (second != 'fresh')
    else:
        assert store.lp_session(sid)['needs_attention_recovery_due'] == (second != 'fresh')
    run()
    assert len(reads) == prior_reads + (second != 'archive')  # Bounded minute retry, including failures.
    assert len(calls) == (2 if second == 'fresh' else 1)


@pytest.mark.parametrize('interleave', ['archive', 'trade', 'identity', 'read_only'])
def test_terminal_verification_delayed_read_preserves_fences_and_business(tmp_path, monkeypatch, interleave, request):
    from timing_support import run_test_in_subprocess
    if run_test_in_subprocess(request):
        return
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy
    from threading import Event
    from tests.test_lp_auto_pool import setup

    engine, exchange, service, store = setup(tmp_path)
    current = [service._now()]
    service.clock = lambda: current[0]
    calls = []
    class Notifier:
        def notify(self, title, message): calls.append((title, message))
    engine._notifier = Notifier()
    engine.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 1})
    engine.lp_auto_set_desired_running(True)
    row = engine.lp_auto_run_once()['intents'][0]
    sid, iid, pool = row['session_id'], row['intent_id'], engine._auto_pool
    exchange.orders[0]['status'] = 'CANCELED'
    engine.lp_tick()
    wait_attention(service)
    first = current[0]
    pool._update(lambda d: d['intents'][iid].update(
        attention_episode='terminal:fault', attention_since=first.isoformat(),
        attention_delivered_channels=['Notifier'], attention_recovery_due=True,
        attention_recovery_ready_since=first.isoformat(), attention_recovery_first_checked_at=first.isoformat()))
    before = deepcopy(pool._read()['intents'][iid])
    session = store.lp_session(sid)
    current[0] += timedelta(seconds=60)
    entered, release = Event(), Event()
    original = exchange.lp_snapshot
    def snapshot(request):
        result = deepcopy(original(request))
        result['account']['checked_at'] = current[0]
        entered.set()
        assert release.wait(5)
        return result
    exchange.lp_snapshot = snapshot
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(engine.lp_auto_reconcile_unknown)
        try:
            assert entered.wait(5)
            # The writer is free while the transport is deliberately delayed.
            store.lp_update_session(sid, patch={'scoring_status': 'unknown'})
            service.flush_session_recovery(sid)
            pool.flush_attention(sid)
            assert calls == []
            if interleave == 'archive':
                store.lp_update_session(sid, patch={'account_baseline_archive': {'id': 'archive'}})
                pool._update(lambda d: d['intents'][iid].update(account_baseline_archive={'id': 'archive'}))
            elif interleave == 'trade':
                store.lp_create_session('unrelated-new-order', 'unrelated-new-order', state='entry_open', payload={})
                store.lp_register_trade_change('unrelated-new-order')
            elif interleave == 'identity':
                store.lp_update_session(sid, patch={'token_id': 'changed-token'})
        finally:
            release.set()
        future.result(timeout=5)
    wait_attention(service)
    after = pool._read()['intents'][iid]
    for key in ('settled', 'report_pending', 'state', 'reserved_usd', 'inventory_cost_usd', 'realized_pnl_usd', 'financial_status'):
        assert after.get(key) == before.get(key)
    saved = store.lp_session(sid)
    for key in ('state', 'facts_checked_at', 'position_reconciled', 'orders_terminal', 'residual_quantity', 'buy_cost'):
        assert saved.get(key) == session.get(key)
    assert len(exchange.posts) == 1
    assert len(calls) == (1 if interleave == 'read_only' else 0)
    assert bool(after.get('attention_recovery_due')) == (interleave != 'read_only')


@pytest.mark.parametrize('restart', ['partial_channels', 'partial_sqlite_ack', 'cache_ack_before_restart'])
def test_mixed_legacy_recovery_defaults_keep_known_channels_through_restart(tmp_path, monkeypatch, restart):
    from pathlib import Path
    from open_trader.notifications import CompositeNotifier, FeishuAppNotifier, XiaoaiSSHNotifier
    from open_trader.polymarket_lp_auto import LPAutoPool
    from tests.test_lp_auto_pool import setup

    engine, _, service, store = setup(tmp_path, 2)
    current = [service._now()]
    service.clock = lambda: current[0]
    calls, voice_fail = [], [True]
    def post(url, payload, headers, timeout):
        if url.endswith('tenant_access_token/internal'):
            return {'code': 0, 'tenant_access_token': 'offline-test-token'}
        calls.append(('feishu_app', payload['content'], payload['uuid']))
        return {'code': 0}
    class Xiaoai(XiaoaiSSHNotifier):
        def __init__(self): super().__init__(host='fake', ssh_key=Path('/tmp/unused-key'))
        def notify(self, title, message):
            calls.append(('xiaoai', message, None))
            if voice_fail[0]: raise RuntimeError('voice unavailable')
    engine._notifier = CompositeNotifier([
        FeishuAppNotifier(app_id='offline-app', app_secret='offline-placeholder',
            receive_id_type='chat_id', receive_id='offline-chat', post_json=post), Xiaoai()])
    engine.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 2})
    engine.lp_auto_set_desired_running(True)
    rows = engine.lp_auto_run_once()['intents']
    wait_attention(service)  # Finish startup delivery before injecting recovery/ACK fixtures.
    pool = engine._auto_pool
    for row, title in zip(rows, ('市场甲', '市场乙')):
        store.lp_update_session(row['session_id'], patch={'market_title': title})
        pool._update(lambda d, row=row: d['intents'][row['intent_id']].update(
            attention_episode=row['intent_id']+':fault', attention_since=current[0].isoformat(),
            attention_notified=True, attention_recovery_due=True,
            attention_recovery_ready_since=(current[0]-timedelta(seconds=60)).isoformat(),
            attention_recovery_first_checked_at=(current[0]-timedelta(seconds=60)).isoformat(),
            checked_at=current[0].isoformat(), financial_status='known', reconcile_error=None, reconcile_reason=None))
    a, b = (row['intent_id'] for row in rows)
    pool._update(lambda d: d['intents'][a].update(attention_delivered_channels=['feishu_app']))
    # B is a pre-channel-schema notified episode. Missing channel fields still
    # use configured default targets, including the real Feishu App channel name.
    assert not pool._read()['intents'][b].get('attention_delivered_channels')
    if restart == 'cache_ack_before_restart':
        # A legacy/polluted recovery ACK cannot expand A's known fault set.
        pool._update(lambda d: d['intents'][a].update(
            attention_recovery_attempted_channels=['feishu_app', 'xiaoai'],
            attention_recovery_delivered_channels=['xiaoai']))
    if restart != 'partial_channels':
        finish = pool._finish_attention_delivery
        busy = [True]
        def partial_ack(outcomes, **kwargs):
            if busy:
                busy.pop()
                assert len(pool._attention_delivery_results) == 2
                acknowledged = {key: value for key, value in outcomes.items() if key[0] == a}
                finish(acknowledged, **kwargs)
                raise sqlite3.OperationalError('database is locked after A ACK')
            return finish(outcomes, **kwargs)
        monkeypatch.setattr(pool, '_finish_attention_delivery', partial_ack)
        with pytest.raises(sqlite3.OperationalError):
            pool.flush_attention()
    else:
        pool.flush_attention()
    assert len(calls) == 2
    feishu, voice = calls
    assert feishu[0] == 'feishu_app' and '市场甲' in feishu[1] and '市场乙' in feishu[1]
    assert voice[0] == 'xiaoai' and '市场乙' in voice[1] and '市场甲' not in voice[1]
    state = pool._read()['intents']
    assert not state[a].get('attention_recovery_due') and state[b]['attention_recovery_due']
    group = state[b]['attention_recovery_delivery_group']
    if restart == 'cache_ack_before_restart':
        current[0] += timedelta(seconds=61)
        pool.flush_attention()  # Expired facts allow only cached ACK, no delivery.
        assert len(calls) == 2
        stored = pool._read()['intents'][b]
        assert stored['attention_recovery_attempted_channels'] == ['feishu_app', 'xiaoai']
        assert stored['attention_recovery_delivered_channels'] == ['feishu_app']
    # Rebuild the real pool instance from its durable SQLite document: no cache
    # can survive this restart. An unacknowledged B result remains unknown;
    # its native UUID must remain stable if B/Feishu is retried after cache loss.
    engine._auto_pool = pool = LPAutoPool(engine)
    service._facts_attention_flusher = pool.flush_attention
    assert not pool._attention_delivery_results
    current[0] += timedelta(seconds=61)
    pool._update(lambda d: d['intents'][b].update(checked_at=current[0].isoformat()))
    assert pool._read()['intents'][b]['attention_recovery_delivery_group'] == group
    voice_fail[0] = False
    pool.flush_attention()
    state = pool._read()['intents']
    assert not state[a].get('attention_recovery_due') and not state[b].get('attention_recovery_due')
    for channel, message, native_uuid in calls[2:]:
        assert '市场乙' in message and '市场甲' not in message
        if channel == 'feishu_app': assert native_uuid == feishu[2]
    # Durable partial-channel ACK and a completed cached ACK retry need only
    # voice after restart; lost, unacknowledged B success may retry B alone.
    assert [call[0] for call in calls[2:]] == (['feishu_app', 'xiaoai'] if restart == 'partial_sqlite_ack' else ['xiaoai'])
    pool.flush_attention()
    assert len(calls) == (4 if restart == 'partial_sqlite_ack' else 3)


@pytest.mark.parametrize('cached_success', ['feishu', 'xiaoai'])
def test_cached_recovery_ack_is_fenced_by_original_fault_channels(tmp_path, monkeypatch, cached_success):
    from copy import deepcopy
    from open_trader.notifications import FeishuWebhookNotifier
    from tests.test_lp_auto_pool import setup

    engine, _, service, _ = setup(tmp_path)
    calls = []
    class Feishu(FeishuWebhookNotifier):
        def __init__(self): pass
        def notify(self, title, message): calls.append(message)
    engine._notifier = Feishu()
    engine.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 1})
    engine.lp_auto_set_desired_running(True)
    row = engine.lp_auto_run_once()['intents'][0]
    wait_attention(service)  # Finish startup delivery before injecting the ACK image.
    pool, iid, episode = engine._auto_pool, row['intent_id'], 'fault'
    pool._update(lambda d: d['intents'][iid].update(
        attention_episode=episode, attention_since=service._now().isoformat(),
        attention_recovery_due=True, attention_delivered_channels=['feishu'],
        attention_recovery_ready_since=(service._now()-timedelta(seconds=60)).isoformat(),
        attention_recovery_first_checked_at=(service._now()-timedelta(seconds=60)).isoformat()))
    pool._attention_delivery_results[(iid, True, episode)] = ({'feishu', 'xiaoai'}, {cached_success})
    images = []
    finish = pool._finish_attention_delivery
    def observe_ack(*args, **kwargs):
        finish(*args, **kwargs)
        images.append(deepcopy(engine.lp_auto_state()['intents'][0]))
    monkeypatch.setattr(pool, '_finish_attention_delivery', observe_ack)
    pool.flush_attention()
    assert images
    first = images[0]
    if cached_success == 'feishu':
        assert not first.get('attention_recovery_due') and calls == []
    else:
        assert first['attention_recovery_attempted_channels'] == ['feishu']
        assert first['attention_recovery_delivered_channels'] == []
        assert first['attention_recovery_due'] and len(calls) == 1
    assert not engine.lp_auto_state()['intents'][0].get('attention_recovery_due')


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('failure', [False, True])
def test_terminal_monitor_verification_does_not_block_or_abort_active_session(tmp_path, monkeypatch, request, automatic, failure):
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy
    from threading import Event
    from timing_support import run_test_in_subprocess
    from tests.test_lp_auto_pool import setup

    if run_test_in_subprocess(request):
        return
    engine, exchange, service, store = setup(tmp_path, 2)
    exchange.get_order_scoring = lambda order_id: True
    current = [service._now()]
    service.clock = lambda: current[0]
    engine.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 2})
    engine.lp_auto_set_desired_running(True)
    rows = engine.lp_auto_run_once()['intents']
    terminal, active = rows
    pool = engine._auto_pool
    exchange.orders[0]['status'] = 'CANCELED'
    engine.lp_tick()
    wait_attention(service)
    sid, iid = terminal['session_id'], terminal['intent_id']
    assert store.lp_session(sid)['state'] == 'complete'
    first = current[0]
    if automatic:
        pool._update(lambda d: d['intents'][iid].update(
            attention_episode='fault', attention_since=first.isoformat(),
            attention_delivered_channels=['feishu'], attention_recovery_due=True,
            attention_recovery_ready_since=first.isoformat(), attention_recovery_first_checked_at=first.isoformat()))
    else:
        # Keep two real LP sessions and remove only the terminal auto attribution
        # to exercise the manual terminal scheduler at the same monitor boundary.
        pool._update(lambda d: d['intents'].pop(iid))
        store.lp_update_session(sid, patch={
            'needs_attention_recovery_due': True, 'needs_attention_recovery_episode': 'fault',
            'needs_attention_recovery_channels': ['feishu'],
            'needs_attention_recovery_ready_since': first.isoformat(),
            'needs_attention_recovery_first_checked_at': first.isoformat()})
    service.set_protection_notifier(lambda *args, **kwargs: False)
    before = deepcopy(store.lp_session(sid))
    projection = engine.lp_auto_state()
    posts, actions, generation = deepcopy(exchange.posts), store.lp_actions(sid), store.lp_trade_generation()
    current[0] += timedelta(seconds=60)
    entered, active_read, release = Event(), Event(), Event()
    original = exchange.lp_snapshot
    def snapshot(req):
        if req['token_id'] == terminal['token_id']:
            entered.set()
            assert release.wait(5)
        else:
            active_read.set()
        result = deepcopy(original(req))
        result['account']['checked_at'] = current[0]
        result['book']['received_at'] = current[0]
        return result
    exchange.lp_snapshot = snapshot
    if failure:
        verify = service.verify_session_recovery
        def explode(selected, **kwargs):
            if selected == sid:
                entered.set()
                raise RuntimeError('terminal verifier failed')
            return verify(selected, **kwargs)
        monkeypatch.setattr(service, 'verify_session_recovery', explode)
    with ThreadPoolExecutor(max_workers=1) as workers:
        tick = workers.submit(engine.lp_tick)
        try:
            assert entered.wait(5)
            assert active_read.wait(2), 'terminal notification read starved the active session'
            result = tick.result(timeout=2)
            assert result['state'] == 'entry_open'
            assert datetime.fromisoformat(store.lp_session(active['session_id'])['facts_checked_at'].replace('Z', '+00:00')) == current[0]
            assert not release.is_set()  # The active tick completes while terminal I/O stays held.
        finally:
            release.set()
        tick.result(timeout=5)
    wait_attention(service)
    after = store.lp_session(sid)
    for key in ('state', 'facts_checked_at', 'position_reconciled', 'orders_terminal', 'residual_quantity', 'buy_cost'):
        assert after.get(key) == before.get(key)
    assert exchange.posts == posts and store.lp_actions(sid) == actions and store.lp_trade_generation() == generation
    after_projection = engine.lp_auto_state()
    assert {k: v for k, v in projection['funds'].items() if k != 'as_of'} == {k: v for k, v in after_projection['funds'].items() if k != 'as_of'}
    assert projection['slots'] == after_projection['slots']
    if automatic:
        assert pool._read()['intents'][iid]['settled'] is True


@pytest.mark.parametrize('archive', ['state', 'metadata'])
@pytest.mark.parametrize('recovery', [False, True])
@pytest.mark.parametrize('cached', [False, True])
def test_manual_late_and_cached_ack_leave_archive_image_unchanged(tmp_path, monkeypatch, archive, recovery, cached):
    from copy import deepcopy
    service, store, current, calls = notice_service(tmp_path)
    arm_fault(store, 'one', '市场一')
    episode = 'one:fault'
    if recovery:
        store.lp_update_session('one', state='entry_open', patch={
            'needs_attention_due': False, 'needs_attention_recovery_due': True,
            'needs_attention_recovery_episode': episode, 'needs_attention_recovery_channels': ['feishu'],
            'needs_attention_recovery_ready_since': (current[0]-timedelta(seconds=60)).isoformat(),
            'needs_attention_recovery_first_checked_at': (current[0]-timedelta(seconds=60)).isoformat(),
            'position_reconciled': True, 'facts_checked_at': current[0].isoformat()})
    archived = []
    def archive_now():
        if archive == 'state':
            row = store.lp_update_session('one', state='account_baseline_archived', patch={})
        else:
            row = store.lp_update_session('one', patch={'account_baseline_archive': {'manifest_id': 'archive'}})
        archived.append(deepcopy(row))
    def send(title, message, voice, *, channels):
        calls.append((title, message, channels))
        if not cached:
            archive_now()  # Network callback runs outside the SQLite writer.
        return {channel: True for channel in channels}
    service.set_protection_notifier(send)
    finish = store.lp_finish_attention_notification
    if cached:
        def busy(*args, **kwargs): raise sqlite3.OperationalError('database is locked')
        monkeypatch.setattr(store, 'lp_finish_attention_notification', busy)
    flush = service.flush_session_recovery if recovery else service.flush_session_attention
    if cached:
        with pytest.raises(sqlite3.OperationalError):
            flush('one')
        assert service._attention_delivery_results
        archive_now()
        monkeypatch.setattr(store, 'lp_finish_attention_notification', finish)
        current[0] += timedelta(seconds=61)
        flush('one')
    else:
        flush('one')
    assert len(calls) == 1
    assert store.lp_session('one') == archived[0]  # Payload, state, revision and timestamps all preserved.
    result = finish('one', recovery=recovery, episode=episode, results={'feishu': True, 'xiaoai': True})
    assert result == archived[0] and store.lp_session('one') == archived[0]


@pytest.mark.parametrize('owner_mode', ['notification', 'monitor', 'report'])
@pytest.mark.parametrize('failure', [False, True])
def test_notification_flight_cannot_be_upgraded_by_stale_queued_monitor(tmp_path, monkeypatch, request, owner_mode, failure):
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy
    from threading import Event
    from timing_support import run_test_in_subprocess
    from tests.test_lp_auto_pool import setup
    from tests.test_lp_reconciliation import _observe_facts_join

    if run_test_in_subprocess(request):
        return
    notification_owner = owner_mode == 'notification'
    engine, exchange, service, store = setup(tmp_path)
    current = [service._now()]
    service.clock = lambda: current[0]
    exchange.get_order_scoring = lambda order_id: True
    engine.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 1})
    engine.lp_auto_set_desired_running(True)
    row = engine.lp_auto_run_once()['intents'][0]
    sid, iid = row['session_id'], row['intent_id']
    stale_pair = store.lp_session_with_revision(sid)
    assert stale_pair[0]['state'] == 'entry_open'
    exchange.orders[0]['status'] = 'CANCELED'
    engine.lp_tick()
    wait_attention(service)
    assert store.lp_session(sid)['state'] == 'complete'
    before = deepcopy(store.lp_session(sid))
    intent = deepcopy(engine._auto_pool._read()['intents'][iid])
    projection = engine.lp_auto_state()
    posts, actions, generation = deepcopy(exchange.posts), store.lp_actions(sid), store.lp_trade_generation()
    current[0] += timedelta(seconds=60)
    entered, release = Event(), Event()
    original = exchange.lp_snapshot
    reads, applies = [], []
    def snapshot(req):
        reads.append(req['token_id'])
        entered.set()
        assert release.wait(5)
        if failure:
            raise OSError('terminal verification unavailable')
        result = deepcopy(original(req))
        result['account']['checked_at'] = current[0]
        result['book']['received_at'] = current[0]
        return result
    exchange.lp_snapshot = snapshot
    apply = service._apply_tick_snapshot
    def observed_apply(*args, **kwargs):
        applies.append(True)
        return apply(*args, **kwargs)
    monkeypatch.setattr(service, '_apply_tick_snapshot', observed_apply)
    locks = (engine._acquire_global_lock, engine._release_global_lock)
    with ThreadPoolExecutor(max_workers=2) as workers:
        if owner_mode == 'report':
            owner = workers.submit(service.reconcile_facts, sid, report_only=True, apply_lock=locks)
        else:
            owner = workers.submit(service.verify_session_recovery, sid, apply_lock=locks) if notification_owner else workers.submit(service._tick_session, stale_pair, apply_lock=locks)
        try:
            assert entered.wait(5)
            joined = _observe_facts_join(service, monkeypatch)
            peer = workers.submit(service.verify_session_recovery, sid, apply_lock=locks) if owner_mode == 'monitor' else workers.submit(service._tick_session, stale_pair, apply_lock=locks)
            assert joined.wait(5), 'stale queued monitor / verifier did not reach the real held flight'
        finally:
            release.set()
        owner_result, peer_result = owner.result(timeout=5), peer.result(timeout=5)
    wait_attention(service)
    assert reads == [row['token_id']], 'both consumers must share exactly one venue read'
    after = store.lp_session(sid)
    if notification_owner:
        assert applies == [], 'notification verification must never enter ordinary monitor apply'
        assert peer_result['state'] == 'complete'
        for key in ('state', 'facts_checked_at', 'position_reconciled', 'orders_terminal', 'residual_quantity', 'buy_cost', 'facts_error'):
            assert after.get(key) == before.get(key)
        after_intent = engine._auto_pool._read()['intents'][iid]
        for key in ('settled', 'report_pending', 'state', 'reserved_usd', 'inventory_cost_usd', 'realized_pnl_usd', 'financial_status'):
            assert after_intent.get(key) == intent.get(key)
        after_projection = engine.lp_auto_state()
        assert {k: v for k, v in projection['funds'].items() if k != 'as_of'} == {k: v for k, v in after_projection['funds'].items() if k != 'as_of'}
        assert projection['slots'] == after_projection['slots']
    else:
        # A joining notifier cannot downgrade the established ordinary monitor
        # owner. Preserve its existing success/failure business-apply contract.
        assert applies == [True]
        owner_status = owner_result[4] if owner_mode == 'report' else owner_result
        assert owner_status['state'] == ('needs_attention' if failure else 'complete')
    assert exchange.posts == posts and store.lp_actions(sid) == actions and store.lp_trade_generation() == generation
