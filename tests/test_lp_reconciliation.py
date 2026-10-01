"""Shared LP facts must recover without weakening the automatic funds fence."""

import pytest

from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread

from tests.test_lp_auto_pool import setup


def _observe_facts_join(lp, monkeypatch):
    """Observe selection of the existing lane, not just thread submission."""
    with lp._facts_lock:
        flights = tuple(lp._facts_inflight.values())
    assert len(flights) == 1
    future = flights[0]['future']
    joined = Event()
    original = future.result

    def result(*args, **kwargs):
        joined.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(future, 'result', result)
    return joined


def test_reward_refresh_during_account_read_does_not_invalidate_funds(tmp_path):
    execution, exchange, lp, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd="100", target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    session_id = execution.lp_auto_run_once()["intents"][0]["session_id"]
    read = exchange.lp_snapshot
    exchange.lp_reward_snapshot = lambda *args: {}

    def snapshot(request):
        result = read(request)
        lp.refresh_rewards(session_id)
        return result

    exchange.lp_snapshot = snapshot
    result = execution.lp_auto_reconcile_unknown()

    assert result["funds"]["status"] == "known"
    assert Decimal(result["funds"]["buy_reserved_usd"]) == 8
    assert Decimal(result["funds"]["available_usd"]) == 92
    assert len(exchange.posts) == 1


def test_manual_read_failure_arms_attention_after_five_minutes(tmp_path, monkeypatch):
    from datetime import timedelta

    from tests import test_lp_auto_pool as venue
    from tests.test_lp_auto_pool import _manual_request

    execution, exchange, lp, store = setup(tmp_path)
    current = [venue.NOW]
    lp.clock = lambda: current[0]
    store.lp_create_session(
        "manual-attention",
        "manual-attention",
        state="entry_open",
        payload={
            **_manual_request(venue.NOW),
            "entry_order_id": "o1",
            "owned_order_ids": ["o1"],
            "submit_status": "accepted",
        },
    )
    notices = []
    lp.set_protection_notifier(
        lambda title, message, xiaoai: notices.append((title, message, xiaoai)) or True
    )

    def unavailable(request):
        raise OSError("account transport failure")

    exchange.lp_snapshot = unavailable
    assert execution.lp_tick()["state"] == "needs_attention"
    monkeypatch.setattr(venue, "NOW", venue.NOW + timedelta(seconds=301))
    current[0] = venue.NOW
    assert execution.lp_tick()["state"] == "needs_attention"
    thread = lp._attention_thread
    if thread is not None:
        thread.join(timeout=2)

    row = store.lp_session("manual-attention")
    assert row["facts_error"] == "external_snapshot_unknown"
    assert row["needs_attention_since"] is not None
    assert row["needs_attention_notified"] is True
    assert len(notices) == 1


def test_rejected_account_identity_cannot_authorize_queue_cancel(
    tmp_path, monkeypatch
):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from open_trader.prediction_arbitrage_execution import PredictionExecutionService
    from tests import test_polymarket_lp as lp_tests
    from tests.test_polymarket_lp import _queue_receipt

    lp_tests._seed_default_lp_history_for_legacy_submits.__wrapped__(
        monkeypatch, SimpleNamespace(function=lambda: None)
    )
    store, exchange, lp, started = lp_tests._queue_running_service(
        tmp_path,
        datetime(2026, 9, 14, 12, tzinfo=UTC),
        key="lp-identity-reconcile",
    )

    now = datetime(2026, 9, 14, 12, tzinfo=UTC)
    exchange.config = SimpleNamespace(wallet_address="correct-wallet")
    execution = PredictionExecutionService(
        store=store,
        monitor=SimpleNamespace(),
        trading=exchange,
        notifier=SimpleNamespace(),
        lock_path=tmp_path / "execution.lock",
        lp=lp,
    )
    execution._breaker_open = False
    snapshot = lp_tests._queue_runtime_snapshot(
        now,
        bid_size="4000",
        open_orders=[_queue_receipt("order-1")],
    )
    snapshot["account"].update(
        authenticated=True,
        wallet_address="wrong-wallet",
        checked_at=now,
        open_orders_complete=True,
        positions_complete=True,
    )
    exchange.snapshot_value = snapshot

    result = execution.lp_tick()

    assert result["state"] == "needs_attention"
    assert store.lp_session(str(started["session_id"]))["facts_error"] == (
        "account_identity_mismatch"
    )
    assert exchange.cancels == []


@pytest.mark.parametrize("auto_first", [False, True])
def test_tick_and_auto_share_one_inflight_venue_read(tmp_path, auto_first, monkeypatch):
    execution, exchange, lp, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd="100", target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    entered, release, duplicate = Event(), Event(), Event()
    read = exchange.lp_snapshot

    def delayed(request):
        if entered.is_set():
            duplicate.set()
        entered.set()
        assert release.wait(5)
        return read(request)

    exchange.lp_snapshot = delayed
    scored = []
    exchange.get_order_scoring = lambda order_id: scored.append(order_id) or True
    first, second = (execution.lp_auto_reconcile_unknown, execution.lp_tick) if auto_first else (execution.lp_tick, execution.lp_auto_reconcile_unknown)
    with ThreadPoolExecutor(2) as workers:
        tick = workers.submit(first)
        try:
            assert entered.wait(2)
            joined = _observe_facts_join(lp, monkeypatch)
            automatic = workers.submit(second)
            assert joined.wait(2), "second consumer must join the in-flight lane"
            assert not duplicate.is_set(), "two independent account reads raced"
        finally:
            release.set()
        tick.result(timeout=5)
        automatic.result(timeout=5)
    assert execution.lp_auto_state()["funds"]["status"] == "known"
    assert len(exchange.posts) == 1
    assert scored == ["o1"], "joining tick must still apply its monitoring work"
    assert not duplicate.is_set(), "drained consumers must still have shared one read"


def test_manual_cancel_invalidates_funds_before_network_and_fences_old_read(tmp_path):
    execution, exchange, lp, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    entered, release = Event(), Event()
    cancel_calls = []
    read = exchange.lp_snapshot

    def delayed(request):
        from copy import deepcopy
        result = deepcopy(read(request))
        entered.set()
        assert release.wait(5)
        return result

    def cancel(ids):
        state = execution.lp_auto_state()
        assert state['funds']['status'] == 'unknown'
        assert Decimal(state['funds']['buy_reserved_usd']) == 8
        return dict(canceled=list(ids), not_canceled={})

    exchange.lp_snapshot = delayed
    exchange.cancel_orders_detailed = cancel
    with ThreadPoolExecutor(1) as workers:
        reading = workers.submit(execution.lp_auto_reconcile_unknown)
        try:
            assert entered.wait(2)
            execution.lp_cancel_orders(dict(confirm=True, order_ids=['o1']))
        finally:
            release.set()
        reading.result(timeout=5)
    assert execution.lp_auto_state()['funds']['status'] == 'unknown'
    assert len(exchange.posts) == 1
    exchange.lp_snapshot = read
    exchange.orders[0]['status'] = 'CANCELED'
    execution.lp_tick()
    assert store.lp_session(sid)['state'] == 'complete'
    assert execution.lp_auto_state()['funds']['status'] == 'known'
    assert Decimal(execution.lp_auto_state()['funds']['buy_reserved_usd']) == 0


def test_fresh_funds_survive_refresh_but_expire_and_recovery_wakes_scheduler(tmp_path, monkeypatch):
    from datetime import timedelta
    from tests import test_lp_auto_pool as venue
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler

    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    execution.lp_auto_set_desired_running(False)
    scheduler_now = venue.NOW
    scheduler = LPAutoScheduler(execution, clock=lambda: scheduler_now)
    assert scheduler.run_due()
    assert not scheduler.run_due()
    entered, release = Event(), Event()
    read = exchange.lp_snapshot

    def delayed(request):
        entered.set()
        assert release.wait(5)
        return read(request)

    exchange.lp_snapshot = delayed
    with ThreadPoolExecutor(1) as workers:
        tick = workers.submit(execution.lp_tick)
        try:
            assert entered.wait(2)
            assert execution.lp_auto_state()['funds']['status'] == 'known'
            monkeypatch.setattr(venue, 'NOW', venue.NOW + timedelta(seconds=61))
            assert execution.lp_auto_state()['funds']['status'] == 'unknown'
            exchange.orders[0]['status'] = 'CANCELED'
        finally:
            release.set()
        tick.result(timeout=5)
    assert execution.lp_auto_state()['funds']['status'] == 'known'
    assert scheduler.run_due(), 'recovery should not wait another polling interval'
    assert not scheduler.run_due(), 'one recovery produces a coalesced wake'
    assert len(exchange.posts) == 1


def test_slow_session_does_not_delay_other_session_publication(tmp_path):
    import time
    execution, exchange, _, _ = setup(tmp_path, 3)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=3))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    execution.lp_auto_set_desired_running(False)
    entered, release = Event(), Event()
    read = exchange.lp_snapshot

    def delayed(request):
        # The store returns newest first. Stall that first session.
        if request['token_id'] == 'm02':
            entered.set()
            assert release.wait(5)
        return read(request)

    exchange.lp_snapshot = delayed
    for order in exchange.orders:
        order['status'] = 'CANCELED'
    with ThreadPoolExecutor(1) as workers:
        tick = workers.submit(execution.lp_tick)
        try:
            assert entered.wait(2)
            deadline = time.monotonic() + 2
            while execution.lp_auto_state()['slots']['occupied'] > 1 and time.monotonic() < deadline:
                time.sleep(.01)
            assert execution.lp_auto_state()['slots']['occupied'] == 1
            assert not tick.done(), 'slow session is still being read'
        finally:
            release.set()
        tick.result(timeout=5)
    assert execution.lp_auto_state()['slots']['occupied'] == 0


def test_apply_lock_release_publishes_other_session_without_second_read(tmp_path):
    import fcntl

    execution, exchange, _, _ = setup(tmp_path, 3)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=3))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    execution.lp_auto_set_desired_running(False)
    for order in exchange.orders:
        order['status'] = 'CANCELED'

    reads = []
    read = exchange.lp_snapshot

    def counting_read(request):
        token = request['token_id']
        reads.append(token)
        return read(request)

    exchange.lp_snapshot = counting_read
    lock_path = tmp_path / 'execution.lock'
    with lock_path.open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        execution.lp_tick()

    assert execution.lp_auto_state()['slots']['occupied'] == 3
    lock_path.unlink(missing_ok=True)
    execution.lp_tick()

    assert execution.lp_auto_state()['slots']['occupied'] == 0
    assert sorted(reads) == ['m00', 'm01', 'm02'], 'release must publish retained facts without a second venue read'


def test_monitor_apply_serializes_with_retained_fact_publication(tmp_path, monkeypatch):
    import fcntl
    import inspect

    execution, exchange, _, _ = setup(tmp_path, 3)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=3))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    execution.lp_auto_set_desired_running(False)
    for order in exchange.orders:
        order['status'] = 'CANCELED'
    reads = []
    read = exchange.lp_snapshot

    def counting_read(request):
        reads.append(request['token_id'])
        return read(request)

    exchange.lp_snapshot = counting_read
    lock_path = tmp_path / 'execution.lock'
    with lock_path.open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        execution.lp_tick()
    assert execution.lp_auto_state()['slots']['occupied'] == 3

    apply_entered, release_apply = Event(), Event()
    global_released, release_return = Event(), Event()
    original_acquire = execution._acquire_global_lock
    original_release = execution._release_global_lock
    watch_release = False

    def held_apply_acquire(*args, **kwargs):
        lock = original_acquire(*args, **kwargs)
        caller = inspect.currentframe().f_back
        if lock is not None and caller is not None and caller.f_code.co_name == 'apply_locked':
            apply_entered.set()
            assert release_apply.wait(5)
        return lock

    def observed_release(lock):
        nonlocal watch_release
        original_release(lock)
        if watch_release:
            watch_release = False
            global_released.set()
            assert release_return.wait(5)

    monkeypatch.setattr(execution, '_acquire_global_lock', held_apply_acquire)
    monkeypatch.setattr(execution, '_release_global_lock', observed_release)
    lock_path.unlink(missing_ok=True)
    worker = Thread(target=execution.lp_tick)
    worker.start()
    try:
        assert apply_entered.wait(5)
        assert not execution._lp._facts_apply_lock.acquire(blocking=False), (
            'monitor apply must serialize with retained fact publication'
        )
        watch_release = True
        release_apply.set()
        assert global_released.wait(5)
        assert not execution._lp._facts_apply_lock.acquire(blocking=False), (
            'facts publication must not enter between global and facts-lock release'
        )
    finally:
        release_return.set()
        release_apply.set()
        worker.join(5)
    assert not worker.is_alive()
    assert execution.lp_auto_state()['slots']['occupied'] == 0
    assert sorted(reads) == ['m00', 'm01', 'm02']


@pytest.mark.parametrize("failing_stage", ["acquire", "release"])
def test_apply_lock_exception_still_releases_facts_apply_lock(
    tmp_path, monkeypatch, failing_stage
):
    execution, exchange, lp, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd="100", target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    execution.lp_auto_set_desired_running(False)
    exchange.orders[0]["status"] = "CANCELED"

    release = execution._release_global_lock

    def fail(*args):
        if failing_stage == "release":
            release(*args)
        raise RuntimeError("execution-lock boundary failed")

    if failing_stage == "acquire":
        monkeypatch.setattr(execution, "_acquire_global_lock", fail)
    else:
        monkeypatch.setattr(execution, "_release_global_lock", fail)
    execution.lp_tick()

    assert lp._facts_apply_lock.acquire(blocking=False), (
        'an execution-lock boundary exception must release the facts apply lock'
    )
    lp._facts_apply_lock.release()


def test_incomplete_account_cannot_release_a_canceled_order(tmp_path):
    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    exchange.orders[0]['status'] = 'CANCELED'
    read = exchange.lp_snapshot

    def incomplete(request):
        snapshot = read(request)
        snapshot['account']['positions_complete'] = False
        return snapshot

    exchange.lp_snapshot = incomplete
    result = execution.lp_auto_reconcile_unknown()
    assert result['funds']['status'] == 'unknown'
    assert Decimal(result['funds']['buy_reserved_usd']) == 8
    assert result['slots']['occupied'] == 1
    exchange.lp_snapshot = read
    assert execution.lp_auto_reconcile_unknown()['funds']['status'] == 'known'


@pytest.mark.parametrize("persist_history", [True, False])
def test_review_cancel_omitted_from_snapshot_stays_outside_common_mutex(
    tmp_path, persist_history
):
    from datetime import UTC, datetime

    from tests.test_lp_auto_pool import _manual_request

    execution, exchange, lp, store = setup(tmp_path)
    now = datetime(2026, 9, 27, 8, tzinfo=UTC)
    lp.clock = lambda: now
    store.lp_create_session(
        "review-off-lock",
        "review-off-lock",
        state="review",
        payload={
            **_manual_request(now),
            "entry_order_id": "o1",
            "owned_order_ids": ["o1"],
            "submit_status": "accepted",
            "stop_requested": True,
            "review_status": "awaiting_reconciliation",
            **(
                {
                    "order_history": {
                        "o1": {
                            "order_id": "o1",
                            "status": "LIVE",
                            "side": "BUY",
                            "token_id": "m00",
                            "condition_id": "m00",
                            "price": "0.40",
                            "original_size": "10",
                            "size_matched": "0",
                        }
                    }
                }
                if persist_history
                else {}
            ),
        },
    )
    entered, release = Event(), Event()
    cancel_calls = []
    exchange.lp_snapshot = lambda request: {
        "account": exchange.lp_account_snapshot(),
        "market": exchange.direction("m00")["market"],
        "book": exchange.direction("m00")["book"],
        "orders": [],
        "trades": [],
        "orders_terminal": False,
        "position_flat": True,
    }

    def cancel(order_id):
        cancel_calls.append(order_id)
        entered.set()
        assert release.wait(5)
        return {"canceled": [order_id], "status": "CANCELED"}

    exchange.cancel_order = cancel
    results = []
    worker = Thread(target=lambda: results.append(execution.lp_tick()))
    worker.start()
    try:
        assert entered.wait(5)
        assert lp._mutex.acquire(blocking=False), (
            'durable review cancel must not wait on the common LP mutex'
        )
        lp._mutex.release()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert cancel_calls == ["o1"]
    row = store.lp_session("review-off-lock")
    assert row["entry_cancel_requested"] is True


def test_converged_queue_buy_allows_protected_sell_outside_common_mutex(tmp_path):
    from datetime import UTC, datetime

    from tests.test_lp_auto_pool import _manual_request

    execution, exchange, lp, store = setup(tmp_path)
    now = datetime(2026, 9, 27, 8, tzinfo=UTC)
    lp.clock = lambda: now
    store.lp_create_session(
        "protected-off-lock",
        "protected-off-lock",
        state="review",
        payload={
            **_manual_request(now),
            "quantity": Decimal("100"),
            "reserved_usd": Decimal("40"),
            "entry_order_id": "o1",
            "owned_order_ids": ["o1"],
            "submit_status": "accepted",
            "review_status": "awaiting_reconciliation",
            "order_history": {
                "o1": {
                    "order_id": "o1",
                    "status": "LIVE",
                    "side": "BUY",
                    "token_id": "m00",
                    "condition_id": "m00",
                    "price": "0.40",
                    "original_size": "100",
                    "size_matched": "0",
                }
            },
            "queue_protection": {
                "version": 2,
                "data_failures": 0,
                "levels": {
                    "0.40": {
                        "state": "canceling",
                        "order_id": "o1",
                        "baseline_price": "0.40",
                        "cancel_targets": ["o1"],
                        "cancel_failed": [],
                        "canceled_order_ids": [],
                    }
                },
            },
        },
    )
    entered, release = Event(), Event()
    sell_calls = []
    receipt = {
        "order_id": "o1",
        "status": "MATCHED",
        "side": "BUY",
        "token_id": "m00",
        "condition_id": "m00",
        "price": "0.40",
        "original_size": "100",
        "size_matched": "100",
        "fee": "0",
    }
    exchange.lp_snapshot = lambda request: {
        "account": {
            **exchange.lp_account_snapshot(),
            "positions": [
                {"token_id": "m00", "condition_id": "m00", "size": "100"}
            ],
        },
        "market": exchange.direction("m00")["market"],
        "book": {
            **exchange.direction("m00")["book"],
            "bids": [{"price": ".30", "size": "1000"}],
        },
        "orders": [receipt],
        "trades": [],
        "orders_terminal": True,
        "position_flat": False,
    }

    def protected_sell(**kwargs):
        sell_calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return {"order_id": "sell-o1", "status": "LIVE"}

    exchange.submit_protected_sell = protected_sell
    worker = Thread(target=execution.lp_tick)
    worker.start()
    try:
        assert entered.wait(5)
        assert lp._mutex.acquire(blocking=False), (
            'converged BUY protection SELL must not wait on the common LP mutex'
        )
        lp._mutex.release()
        claimed = store.lp_session("protected-off-lock")
        assert claimed["state"] == "stop_loss_exit"
        assert claimed["stop_loss_latched"] is True
        assert Decimal(str(claimed["residual_quantity"])) == Decimal("100")
        assert claimed["position_reconciled"] is True
        assert claimed["fee_status"] == "known"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(sell_calls) == 1
    assert sell_calls[0]["token_id"] == "m00"


def test_pending_manual_stop_cancel_is_not_resent_by_monitor_review(tmp_path):
    from tests.test_lp_auto_pool import NOW

    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd="100", target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    session_id = execution.lp_auto_run_once()["intents"][0]["session_id"]
    store.lp_update_session(session_id, patch={"review_at": NOW})
    entered, release = Event(), Event()
    calls = []

    def cancel(order_id):
        calls.append(order_id)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return {"not_canceled": {order_id: "venue_not_found"}}

    exchange.cancel_order = cancel
    stopping = Thread(target=execution.lp_stop, args=(session_id,))
    stopping.start()
    try:
        assert entered.wait(5)
        execution.lp_tick()
        assert calls == ["o1"], "a pending exact-ID cancel must not be replayed"
    finally:
        release.set()
        stopping.join(5)
    assert not stopping.is_alive()


def test_slow_scoring_does_not_discard_another_sessions_fresh_funds(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from tests import test_lp_auto_pool as venue

    execution, exchange, lp, store = setup(tmp_path, 2)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=2))
    execution.lp_auto_set_desired_running(True)
    intents = execution.lp_auto_run_once()['intents']
    execution.lp_auto_set_desired_running(False)
    first, second = [store.lp_session(intent['session_id']) for intent in intents]
    entered, release = Event(), Event()

    def scoring(order_id):
        if order_id == first['entry_order_id']:
            entered.set()
            assert release.wait(5)
        return True

    exchange.get_order_scoring = scoring
    apply_lock = (execution._acquire_global_lock, execution._release_global_lock)
    with ThreadPoolExecutor(1) as workers:
        monitoring = workers.submit(lp._tick_session, (first, 0), apply_lock=apply_lock)
        try:
            assert entered.wait(2)
            monkeypatch.setattr(venue, 'NOW', venue.NOW + timedelta(seconds=61))
            result = lp.reconcile_facts(second['session_id'], apply_lock=apply_lock)
            assert result[3] is None, 'scoring must not occupy the facts apply lock'
            refreshed = next(row for row in execution.lp_auto_state()['intents']
                             if row['session_id'] == second['session_id'])
            assert refreshed['financial_status'] == 'known'
            assert datetime.fromisoformat(str(refreshed['checked_at'])) == venue.NOW
            assert not monitoring.done()
        finally:
            release.set()
        monitoring.result(timeout=5)
    assert len(exchange.posts) == 2


def test_partial_fill_collects_remaining_buy_before_scoring(tmp_path):
    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    exchange.orders[0]['size_matched'] = '5'
    exchange.positions = [dict(token_id='m00', condition_id='m00', size='5')]
    calls = []

    def cancel(order_id):
        calls.append('cancel')
        return dict(canceled=[order_id], status='CANCELED')

    exchange.cancel_order = cancel
    exchange.get_order_scoring = lambda order_id: calls.append('scoring') or True
    execution.lp_tick()
    assert calls == ['cancel'], 'fill cleanup must not wait on a scoring read'
    assert len(exchange.posts) == 1


def test_scheduler_reuses_published_facts_and_leaves_settled_history_alone(tmp_path):
    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    execution.lp_auto_set_desired_running(False)
    execution.lp_tick()
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_auto_pool import NOW
    scheduler = LPAutoScheduler(execution, clock=lambda: NOW)
    read = exchange.lp_snapshot
    calls = []
    exchange.lp_snapshot = lambda request: calls.append(request['token_id']) or read(request)
    assert scheduler.run_due()
    assert calls == [], 'scheduler must reuse the facts tick just published'
    exchange.orders[0]['status'] = 'CANCELED'
    execution.lp_tick()
    calls.clear()
    assert scheduler.run_due()
    scheduler.request_check()
    assert scheduler.run_due()
    assert calls == [], 'settled history must leave high-frequency account reads'
    assert execution.lp_auto_state()['funds']['status'] == 'known'


def test_session_and_funds_publication_roll_back_together(tmp_path):
    import sqlite3
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    before = store.lp_session(sid)
    exchange.orders[0]['status'] = 'CANCELED'
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TRIGGER fail_funds BEFORE UPDATE ON lp_auto_pool BEGIN SELECT RAISE(ABORT, 'disk fault'); END")
    assert execution.lp_tick()['state'] == 'error'
    assert store.lp_session(sid) == before
    assert Decimal(execution.lp_auto_state()['funds']['buy_reserved_usd']) == 8
    with sqlite3.connect(store.path) as connection:
        connection.execute('DROP TRIGGER fail_funds')
    execution.lp_tick()
    assert store.lp_session(sid)['state'] == 'complete'
    assert Decimal(execution.lp_auto_state()['funds']['buy_reserved_usd']) == 0


def test_missing_submitted_session_keeps_reservation(tmp_path):
    import sqlite3
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    with sqlite3.connect(store.path) as connection:
        connection.execute('DELETE FROM lp_sessions WHERE session_id=?', (sid,))
    result = execution.lp_auto_run_once()
    assert result['funds']['status'] == 'unknown'
    assert Decimal(result['funds']['buy_reserved_usd']) == 8
    assert result['slots']['occupied'] == 1
    assert len(exchange.posts) == 1


def test_production_snapshot_preserves_unknown_position_lists():
    import pytest
    from tests.test_polymarket_lp import _SDKAccountClient, _SDKPublicClient
    from tests.test_lp_auto_pool import NOW
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
    account = _SDKAccountClient(NOW)
    adapter = PolymarketTradingClient(TradingConfig('0x'+'1'*40, '0x'+'2'*40), account,
                                    public_client_factory=lambda: _SDKPublicClient(NOW))
    request = dict(market_id='market-1', condition_id='0x'+'c'*64, token_id='0x'+'1'*64, outcome='YES')
    assert adapter.lp_snapshot(request)['account']['positions_complete'] is True
    account.list_positions = lambda: [object()]
    assert adapter.lp_snapshot(request)['account']['positions_complete'] is False
    account.list_positions = lambda: None
    with pytest.raises(ValueError, match='external_snapshot_unknown'):
        adapter.lp_snapshot(request)
    account.list_positions = lambda: []
    account.list_open_orders = lambda: [dict(order_id='o', token_id=request['token_id'],
        condition_id=request['condition_id'], side='BUY', status='LIVE', price='.4', original_size='20')]
    snapshot = adapter.lp_snapshot(request)
    assert snapshot['account']['open_orders_complete'] is False
    assert snapshot['orders'][0]['fill_quantity_known'] is False
    account.list_open_orders = lambda: []
    account.list_account_trades = lambda **kwargs: None
    with pytest.raises(ValueError, match='external_snapshot_unknown'):
        adapter.lp_snapshot(request)
    account.list_account_trades = lambda **kwargs: [object()]
    with pytest.raises(ValueError, match='external_snapshot_unknown'):
        adapter.lp_snapshot(request)


@pytest.mark.parametrize('missing_order', [None, 'extra'])
def test_production_snapshot_queries_every_owned_order(missing_order):
    from tests.test_polymarket_lp import _SDKAccountClient, _SDKPublicClient, _request
    from tests.test_lp_auto_pool import NOW
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig

    account = _SDKAccountClient(NOW)
    account.list_open_orders = lambda: []
    account.list_account_trades = lambda **kwargs: []
    request = dict(_request(NOW), entry_order_id='entry', augment_order_ids=['augment'],
                   owned_order_ids=['entry', 'augment', 'extra'])
    queried = []
    def get_order(*, order_id):
        queried.append(order_id)
        if order_id == missing_order:
            raise TimeoutError('receipt unavailable')
        return dict(order_id=order_id, token_id=request['token_id'],
                    condition_id=request['condition_id'], side='BUY', status='CANCELED',
                    price='.3', original_size='10', size_matched='0')
    account.get_order = get_order
    adapter = PolymarketTradingClient(TradingConfig('0x'+'1'*40, '0x'+'2'*40), account,
                                    public_client_factory=lambda: _SDKPublicClient(NOW))
    snapshot = adapter.lp_snapshot(request)
    assert sorted(queried) == ['augment', 'entry', 'extra']
    assert {row['order_id'] for row in snapshot['orders']} == {'entry', 'augment', 'extra'} - {missing_order}
    assert snapshot['orders_terminal'] is (missing_order is None)


@pytest.mark.parametrize('raw_orders', [None, []])
def test_share_watch_distinguishes_missing_orders_from_empty_orders(tmp_path, raw_orders):
    from types import SimpleNamespace
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig

    execution, _, _, _ = setup(tmp_path)
    adapter = PolymarketTradingClient(TradingConfig('signer', 'test-wallet'),
        SimpleNamespace(list_open_orders=lambda: raw_orders))
    execution._trading = adapter
    assert adapter.lp_open_orders_snapshot()['open_orders_complete'] is (raw_orders is not None)
    result = execution.refresh_lp_share_watch()
    assert result['state'] == ('unknown' if raw_orders is None else 'ready')
    if raw_orders is None:
        assert result['reason'] == 'account_facts_unknown'


@pytest.mark.parametrize('trade_kind', ['missing_status', 'missing_attribution', 'bad_maker', 'mined', 'failed'])
def test_production_trade_structure_preserves_funds_until_reconciled(tmp_path, monkeypatch, trade_kind):
    from datetime import datetime
    from types import SimpleNamespace
    from tests.test_lp_auto_pool import NOW
    from open_trader import polymarket_trading
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(polymarket_trading, 'datetime', Clock)
    execution, exchange, lp, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    exchange.orders[0]['status'] = 'CANCELED'
    trade = dict(id='t1', token_id='m00', market='m00', taker_order_id='o1',
                 side='BUY', status='CONFIRMED', price='.4', size='8')
    if trade_kind == 'missing_status':
        trade = dict(id='t1', token_id='m00')
    elif trade_kind == 'missing_attribution':
        trade.pop('taker_order_id')
    elif trade_kind == 'bad_maker':
        trade.update(taker_order_id='foreign', maker_orders=[
            dict(order_id='o1', side='BUY', price='.4', matched_amount='8')])
    else:
        trade['status'] = trade_kind.upper()
    public = SimpleNamespace(
        get_market=lambda **kwargs: dict(id='m00', condition_id='m00', state={'accepting_orders': True},
            outcomes={'yes': {'token_id': 'm00', 'label': 'YES'}}, trading={'fees_enabled': False},
            rewards={'rewards_min_size': '20', 'rewards_max_spread': '10'}),
        get_order_book=lambda **kwargs: {**exchange.direction('m00')['book'],
                                        'tick_size': '.01', 'min_order_size': '20'})
    sdk = SimpleNamespace(list_account_trades=lambda **kwargs: [trade],
                          get_order=lambda **kwargs: exchange.orders[0])
    adapter = PolymarketTradingClient(TradingConfig('signer', 'test-wallet'), sdk,
                                    public_client_factory=lambda: public)
    adapter._account_read_facts = lambda: (
        Decimal(1000), Decimal(1000), [], [], NOW, (trade,), True
    )
    lp.exchange = adapter
    result = execution.lp_auto_reconcile_unknown()
    if trade_kind != 'failed':
        assert store.lp_session(sid)['state'] != 'complete'
        assert result['funds']['status'] == 'unknown'
        assert result['funds']['available_usd'] is None
        assert Decimal(result['funds']['buy_reserved_usd']) == 8
        if trade_kind == 'mined':
            assert store.lp_session(sid)['facts_error'] == 'trade_not_confirmed'
    # A reliable failed trade and zero-fill terminal receipt can settle safely.
    trade.clear()
    trade.update(id='t1', token_id='m00', taker_order_id='o1', status='FAILED')
    result = execution.lp_auto_reconcile_unknown()
    assert store.lp_session(sid)['state'] == 'complete'
    assert result['funds']['status'] == 'known'
    assert Decimal(result['funds']['buy_reserved_usd']) == 0
    assert Decimal(result['funds']['inventory_cost_usd']) == 0
    assert len(exchange.posts) == 1


@pytest.mark.parametrize('incomplete', ['positions_complete', 'open_orders_complete'])
def test_manual_session_rejects_explicitly_incomplete_facts(tmp_path, incomplete):
    from tests.test_polymarket_lp import _Exchange, _request, _snapshot
    from tests.test_lp_auto_pool import NOW
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

    store = PredictionArbitrageStore(tmp_path)
    exchange = _Exchange()
    exchange.snapshot_value = snapshot = _snapshot(NOW)
    snapshot['account'][incomplete] = False
    snapshot['orders'] = [dict(order_id='entry', token_id=_request(NOW)['token_id'],
        side='BUY', status='CANCELED', price='.3', original_size='10', size_matched='0')]
    store.lp_create_session('manual', 'manual', state='entry_open', payload=dict(
        _request(NOW), entry_order_id='entry', owned_order_ids=['entry'], submit_status='accepted'))
    service = PolymarketLPService(store, exchange, clock=lambda: NOW)
    service.tick()
    assert store.lp_session('manual')['state'] != 'complete'
    assert store.lp_session('manual')['facts_error'] == 'account_facts_incomplete'
    snapshot['account'][incomplete] = True
    service.tick()
    assert store.lp_session('manual')['state'] == 'complete'


def _financial_session(tmp_path, *, snapshot, owned_order_ids=('entry',)):
    from datetime import UTC, datetime, timedelta
    from tests.test_polymarket_lp import _Exchange, _request, _snapshot
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

    now = datetime(2026, 9, 29, 8, tzinfo=UTC)
    store = PredictionArbitrageStore(tmp_path)
    exchange = _Exchange()
    exchange.snapshot_value = snapshot
    request = _request(now)
    store.lp_create_session('financial', 'financial', state='entry_open', payload=dict(
        request, entry_order_id='entry', owned_order_ids=list(owned_order_ids), submit_status='accepted'))
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    service.tick()
    return store.lp_session('financial')


def test_zero_inventory_settles_without_live_book_but_fees_stay_fail_closed(tmp_path):
    from datetime import UTC, datetime, timedelta
    from tests.test_polymarket_lp import _snapshot

    now = datetime(2026, 9, 29, 8, tzinfo=UTC)
    settled = _snapshot(now)
    settled.pop('book')
    settled['orders'] = [dict(order_id='entry', token_id=_snapshot(now)['market']['token_id'],
        side='BUY', status='CANCELED', price='.3', original_size='10', size_matched='0')]
    session = _financial_session(tmp_path, snapshot=settled)
    assert session['state'] == 'complete'
    assert session['fee_status'] == 'known'
    assert session['inventory_valuation_status'] == 'known'
    assert session['book_admission_ready'] is False

    valued = _snapshot(now)
    valued.pop('book')
    valued['market'] = {**valued['market'], 'fees_enabled': False}
    valued['account']['positions'] = [dict(asset_id=valued['market']['token_id'], size='10')]
    valued['orders'] = [dict(order_id='entry', token_id=valued['market']['token_id'],
        side='BUY', status='MATCHED', price='.3', original_size='10', size_matched='10')]
    session = _financial_session(tmp_path / 'inventory', snapshot=valued)
    assert session['state'] != 'complete'
    assert session['position_reconciled'] is True
    assert session['inventory_valuation_status'] == 'unknown'
    assert session['residual_exit_value'] is None
    assert session['financial_block_reason'] == 'inventory_valuation_unknown'

    stale = _snapshot(now)
    stale['market'] = {**stale['market'], 'fees_enabled': False}
    stale['account']['positions'] = [dict(asset_id=stale['market']['token_id'], size='10')]
    stale['orders'] = [dict(order_id='entry', token_id=stale['market']['token_id'],
        side='BUY', status='MATCHED', price='.3', original_size='10', size_matched='10')]
    stale['book'] = {**stale['book'], 'received_at': now - timedelta(seconds=61)}
    session = _financial_session(tmp_path / 'stale', snapshot=stale)
    assert session['state'] != 'complete'
    assert session['position_reconciled'] is True
    assert session['inventory_valuation_status'] == 'unknown'
    assert session['residual_exit_value'] is None
    assert session['financial_block_reason'] == 'inventory_valuation_unknown'

    token = _snapshot(now)['market']['token_id']
    unpriced = _snapshot(now)
    unpriced.pop('book')
    unpriced['market'] = {**unpriced['market'], 'fee': '.01', 'fees_enabled': True}
    unpriced['orders'] = [
        dict(order_id='entry', token_id=token, side='BUY', status='CANCELED',
             price='.3', original_size='10', size_matched='1'),
        dict(order_id='exit', token_id=token, side='SELL', status='CANCELED',
             price='.4', original_size='1', size_matched='1'),
    ]
    unpriced['trades'] = [dict(
        trade_id='trade-1', status='CONFIRMED', maker_orders=[
            dict(order_id='entry', token_id=token, side='BUY', matched_amount='1', price='.3'),
            dict(order_id='exit', token_id=token, side='SELL', matched_amount='1', price='.4'),
        ], taker_order_id='external')]
    session = _financial_session(
        tmp_path / 'fees', snapshot=unpriced, owned_order_ids=('entry', 'exit'))
    assert session['state'] != 'complete'
    assert Decimal(session['buy_filled_quantity']) == 1
    assert Decimal(session['sold_quantity']) == 1
    assert Decimal(session['residual_quantity']) == 0
    assert session['fee_status'] == 'unknown'
    assert session['financial_block_reason'] == 'trade_fee_unknown'


def test_failed_account_read_blocks_funds_even_while_execution_lock_is_busy(tmp_path):
    import fcntl
    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()

    def unavailable(request):
        raise OSError('account unavailable')

    exchange.lp_snapshot = unavailable
    with (tmp_path / 'execution.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = execution.lp_auto_reconcile_unknown()
    assert result['funds']['status'] == 'unknown'
    assert Decimal(result['funds']['buy_reserved_usd']) == 8
    assert len(exchange.posts) == 1


def test_tick_recovers_saved_accepted_id_after_receipt_apply_was_busy(tmp_path):
    import fcntl
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    original = exchange.lp_post_order
    with (tmp_path / 'execution.lock').open('a+') as lock:
        def posted(signed):
            result = original(signed)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return result
        exchange.lp_post_order = posted
        result = execution.lp_auto_run_once()
        sid = result['intents'][0]['session_id']
        assert not store.lp_session(sid).get('entry_order_id')
        assert result['funds']['status'] == 'unknown'
    exchange.orders.append(dict(order_id='extra', token_id='m00', condition_id='m00', side='BUY',
        status='CANCELED', original_size='1', size_matched='0', price='.4'))
    store.lp_update_session(sid, patch=dict(owned_order_ids=['extra']))
    execution.lp_tick()
    assert set(store.lp_session(sid)['owned_order_ids']) == {'extra', 'o1'}
    assert store.lp_session(sid)['entry_order_id'] == 'o1'
    assert execution.lp_auto_state()['funds']['status'] == 'known'
    assert len(exchange.posts) == 1


def test_fresh_read_during_manual_cancel_cannot_restore_known_funds(tmp_path):
    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    entered, release = Event(), Event()

    def cancel(ids):
        entered.set()
        assert release.wait(5)
        return dict(canceled=list(ids), not_canceled={})

    exchange.cancel_orders_detailed = cancel
    with ThreadPoolExecutor(1) as workers:
        canceling = workers.submit(execution.lp_cancel_orders, dict(confirm=True, order_ids=['o1']))
        try:
            assert entered.wait(2)
            result = execution.lp_auto_reconcile_unknown()
            assert result['funds']['status'] == 'unknown'
            assert Decimal(result['funds']['buy_reserved_usd']) == 8
        finally:
            release.set()
        canceling.result(timeout=5)
    exchange.orders[0]['status'] = 'CANCELED'
    execution.lp_tick()
    assert execution.lp_auto_state()['funds']['status'] == 'known'
    assert Decimal(execution.lp_auto_state()['funds']['buy_reserved_usd']) == 0
    assert len(exchange.posts) == 1


def test_session_lane_stays_owned_through_post_read_monitoring(tmp_path, monkeypatch):
    execution, exchange, lp, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    entered, release, duplicate = Event(), Event(), Event()
    read = exchange.lp_snapshot
    calls = []

    def snapshot(request):
        calls.append(request['token_id'])
        if len(calls) > 1:
            duplicate.set()
        return read(request)

    def scoring(order_id):
        entered.set()
        assert release.wait(5)
        return True

    exchange.lp_snapshot = snapshot
    exchange.get_order_scoring = scoring
    with ThreadPoolExecutor(2) as workers:
        tick = workers.submit(execution.lp_tick)
        try:
            assert entered.wait(2)
            joined = _observe_facts_join(lp, monkeypatch)
            automatic = workers.submit(execution.lp_auto_reconcile_unknown)
            assert joined.wait(2), 'second consumer must reach the held lane'
            assert not duplicate.is_set(), 'a second owner started before monitoring finished'
        finally:
            release.set()
        tick.result(timeout=5)
        automatic.result(timeout=5)
    assert len(exchange.posts) == 1
    assert calls == ['m00'], 'drain must not hide a second venue read'
    assert not duplicate.is_set()


def test_flat_session_with_unknown_fees_stays_in_tick_reconciliation(tmp_path):
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    exchange.orders[0].update(status='FILLED', size_matched='20')
    exchange.orders.append(dict(order_id='sell', token_id='m00', condition_id='m00', side='SELL',
        status='FILLED', original_size='20', size_matched='20', price='.4'))
    store.lp_update_session(sid, patch=dict(passive_exit_order_id='sell', owned_order_ids=['o1','sell']))
    read = exchange.lp_snapshot

    def unknown_fees(request):
        snapshot = read(request)
        snapshot['market'].update(fees_enabled=True, fee=None)
        return snapshot

    exchange.lp_snapshot = unknown_fees
    execution.lp_tick()
    assert store.lp_session(sid)['state'] != 'complete'
    assert execution.lp_auto_state()['funds']['status'] == 'unknown'
    assert Decimal(execution.lp_auto_state()['funds']['buy_reserved_usd']) == 8
    assert execution.lp_auto_state()['slots']['occupied'] == 1
    exchange.lp_snapshot = read
    execution.lp_tick()
    assert store.lp_session(sid)['state'] == 'complete'
    assert execution.lp_auto_state()['funds']['status'] == 'known'


def test_publish_compare_and_swap_conflict_is_a_retry_not_a_failed_round(tmp_path):
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    publish = store.lp_publish_facts
    raced = []

    def competing_writer(session_id, revision, **kwargs):
        if not raced:
            raced.append(True)
            store.lp_register_trade_change(session_id)
        return publish(session_id, revision, **kwargs)

    store.lp_publish_facts = competing_writer
    result = execution.lp_auto_reconcile_unknown()
    assert result['funds']['status'] == 'unknown'
    assert Decimal(result['funds']['buy_reserved_usd']) == 8
    assert execution.lp_auto_reconcile_unknown()['funds']['status'] == 'known'
    assert len(exchange.posts) == 1


def test_filled_submit_receipt_without_account_fill_facts_keeps_reservation(tmp_path):
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    post = exchange.lp_post_order
    def matched(signed):
        receipt = dict(post(signed), status='MATCHED')
        receipt.pop('size_matched')
        exchange.orders.clear()
        return receipt
    exchange.lp_post_order = matched
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    result = execution.lp_auto_reconcile_unknown()
    assert store.lp_session(sid)['state'] != 'complete'
    assert result['funds']['status'] == 'unknown'
    assert Decimal(result['funds']['buy_reserved_usd']) == 8
    assert result['slots']['occupied'] == 1
    assert len(exchange.posts) == 1


def test_slow_settled_report_does_not_block_active_recovery_or_new_buy(tmp_path):
    from datetime import timedelta
    from tests.test_lp_auto_pool import NOW
    execution, exchange, _, store = setup(tmp_path, 2)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    exchange.orders[0].update(status='FILLED', size_matched='20')
    exchange.orders.append(dict(order_id='sell', token_id='m00', condition_id='m00', side='SELL',
        status='FILLED', original_size='20', size_matched='20', price='.4'))
    store.lp_update_session(sid, patch=dict(passive_exit_order_id='sell', owned_order_ids=['o1','sell']))
    settled = execution.lp_auto_reconcile_unknown()['intents'][0]
    assert settled['settled'] and settled['report_pending']
    store.lp_update_session(sid, patch=dict(facts_checked_at=NOW-timedelta(seconds=301)))
    entered, release = Event(), Event()
    read = exchange.lp_snapshot
    def delayed(request):
        if request.get('entry_order_id') == 'o1':
            entered.set()
            assert release.wait(5)
        return read(request)
    exchange.lp_snapshot = delayed
    with ThreadPoolExecutor(2) as workers:
        assert execution.lp_generate_due_auto_reports() == []
        assert not entered.is_set()
        # Exercise the retained offline primitive without re-enabling its worker.
        report = workers.submit(execution._lp_auto_pool().reconcile_reports)
        try:
            assert entered.wait(2), 'offline report reconciliation must own the low-priority read'
            active = workers.submit(execution.lp_auto_run_once)
            result = active.result(timeout=2)
            assert len(exchange.posts) == 2
            assert result['funds']['status'] == 'known'
        finally:
            release.set()
        report.result(timeout=5)
    assert store.lp_session(sid)['state'] == 'complete'


def test_unknown_cancel_recovers_from_exact_terminal_receipt_after_restart(tmp_path):
    from types import SimpleNamespace
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_execution import PredictionExecutionService
    from tests.test_lp_auto_pool import NOW
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    cancels = []
    def lost_reply(ids):
        cancels.extend(ids)
        exchange.orders[0]['status'] = 'CANCELED'
        raise TimeoutError('cancel reply lost')
    exchange.cancel_orders_detailed = lost_reply
    with pytest.raises(TimeoutError):
        execution.lp_cancel_orders(dict(confirm=True, order_ids=['o1']))
    assert execution.lp_auto_state()['funds']['status'] == 'unknown'
    restarted = PredictionExecutionService(store=store, monitor=SimpleNamespace(), trading=exchange,
        notifier=SimpleNamespace(), lock_path=tmp_path/'execution.lock',
        lp=PolymarketLPService(store, exchange, clock=lambda: NOW))
    restarted._breaker_open = False
    restarted.lp_tick()
    assert store.lp_session(sid)['state'] == 'complete'
    assert restarted.lp_auto_state()['funds']['status'] == 'known'
    assert restarted.lp_auto_state()['slots']['occupied'] == 0
    assert cancels == ['o1'] and len(exchange.posts) == 1


def test_manual_cancel_during_scoring_fences_monitor_apply(tmp_path):
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    scoring, release, canceled = Event(), Event(), Event()
    def score(order_id):
        scoring.set()
        assert release.wait(5)
        return True
    def cancel(ids):
        assert execution.lp_auto_state()['funds']['status'] == 'unknown'
        canceled.set()
        return dict(canceled=list(ids), not_canceled={})
    exchange.get_order_scoring = score
    exchange.cancel_orders_detailed = cancel
    with ThreadPoolExecutor(2) as workers:
        tick = workers.submit(execution.lp_tick)
        try:
            assert scoring.wait(2)
            manual = workers.submit(execution.lp_cancel_orders, dict(confirm=True, order_ids=['o1']))
            assert canceled.wait(2), 'read-only scoring must not block manual cancellation'
        finally:
            release.set()
        tick.result(timeout=5)
        manual.result(timeout=5)
    assert canceled.is_set()
    assert store.lp_session(sid).get('scoring_status') != 'true'
    assert execution.lp_auto_state()['funds']['status'] == 'unknown'
    assert len(exchange.posts) == 1


def test_slow_dashboard_does_not_block_session_and_funds_publication(tmp_path):
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    entered, release = Event(), Event()
    def dashboard_account():
        entered.set()
        assert release.wait(5)
        return exchange.lp_account_snapshot()
    exchange.lp_account_snapshot_shared = dashboard_account
    with ThreadPoolExecutor(2) as workers:
        dashboard = workers.submit(execution.refresh_lp_dashboard_snapshot)
        try:
            assert entered.wait(2)
            exchange.orders[0]['status'] = 'CANCELED'
            result = workers.submit(execution.lp_auto_reconcile_unknown).result(timeout=2)
            assert result['funds']['status'] == 'known'
            assert result['slots']['occupied'] == 0
            assert store.lp_session(sid)['state'] == 'complete'
        finally:
            release.set()
        dashboard.result(timeout=5)


def test_slow_session_does_not_block_another_market_buy(tmp_path):
    execution, exchange, lp, _ = setup(tmp_path, 2)
    second = lp._candidate_pool.pop('m01')
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=2))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    lp._candidate_pool['m01'] = second
    entered, release = Event(), Event()
    read = exchange.lp_snapshot
    calls = []

    def delayed(request):
        if request['token_id'] == 'm00':
            calls.append('m00')
            entered.set()
            assert release.wait(5)
        return read(request)

    exchange.lp_snapshot = delayed
    with ThreadPoolExecutor(1) as workers:
        result = workers.submit(execution.lp_auto_run_once)
        try:
            assert entered.wait(2)
            state = result.result(timeout=2)
            assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01']
            assert Decimal(state['funds']['buy_reserved_usd']) >= 16
            execution.lp_auto_run_once()
            assert calls == ['m00'], 'do not stack reads behind the stalled session'
        finally:
            release.set()
        result.result(timeout=5)


def test_failed_session_keeps_capital_but_allows_other_market_buy(tmp_path):
    execution, exchange, lp, _ = setup(tmp_path, 2)
    second = lp._candidate_pool.pop('m01')
    execution.lp_auto_configure(dict(budget_usd='16', target_buy_count=2))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    lp._candidate_pool['m01'] = second
    read = exchange.lp_snapshot

    def failed(request):
        if request['token_id'] == 'm00':
            raise ValueError('order_schema_unknown')
        return read(request)

    exchange.lp_snapshot = failed
    state = execution.lp_auto_run_once()
    assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01']
    assert Decimal(state['funds']['buy_reserved_usd']) >= 16
    assert state['funds']['status'] == 'unknown', 'do not present partial facts as complete'
    assert Decimal(state['funds']['spendable_usd']) == 0


def test_market_read_capacity_remains_fail_fast_for_shared_snapshot_callers(tmp_path, caplog):
    import logging
    from open_trader.polymarket_trading import _lp_read_stage
    caplog.set_level(logging.INFO, logger="open_trader.polymarket_trading")
    _, exchange, lp, _ = setup(tmp_path, 2)
    first_entered, second_entered = Event(), Event()
    release = Event()
    snapshot = exchange.lp_snapshot
    def blocked(request):
        event = first_entered if request['token_id'] == 'm00' else second_entered
        event.set()
        assert release.wait(5)
        return snapshot(request)
    exchange.lp_snapshot = blocked
    def shared_read(identity):
        lp._facts_owner.session_id = 'test-session'
        try:
            lp._read_snapshot(identity)
        finally:
            lp._facts_owner.session_id = None
    reads = [Thread(target=shared_read, args=(identity,))
             for identity in (dict(condition_id='m00', token_id='m00'),
                              dict(condition_id='m01', token_id='m01'))]
    for read in reads:
        read.start()
    assert first_entered.wait(2) and second_entered.wait(2)
    try:
        lp._facts_owner.session_id = 'test-session'
        with pytest.raises(ValueError, match='market_read_capacity'):
            with _lp_read_stage('facts_read'):
                lp._read_snapshot(dict(condition_id='m02', token_id='m02'))
        assert 'lp_read_wait stage=facts_read' in caplog.text
        assert 'reason=market_read_capacity' in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    finally:
        lp._facts_owner.session_id = None
        release.set()
        for read in reads:
            read.join(2)


def test_market_read_timeout_discards_late_result_and_preserves_other_capacity(tmp_path, caplog, monkeypatch):
    import logging
    from concurrent.futures import Future
    from open_trader import polymarket_lp
    from open_trader.polymarket_trading import _lp_read_stage
    caplog.set_level(logging.INFO, logger="open_trader.polymarket_trading")
    _, exchange, lp, _ = setup(tmp_path, 2)
    healthy_timeout = lp._market_read_timeout
    lp._facts_owner.session_id = 'test-session'
    entered, release = Event(), Event()
    original = exchange.lp_snapshot
    calls, futures = [], []
    clock = [100.0]
    monkeypatch.setattr(polymarket_lp, 'monotonic', lambda: clock[0])

    class ObservedFuture(Future):
        def __init__(self):
            super().__init__()
            futures.append(self)

        def result(self, timeout=None):
            if self is futures[0] and not self.done():
                assert entered.wait(2), 'timeout coverage must reach the blocked read'
            return super().result(timeout=timeout)

    monkeypatch.setattr(polymarket_lp, 'Future', ObservedFuture)

    def delayed(request):
        if request['token_id'] == 'm00':
            calls.append('m00')
            entered.set()
            assert release.wait(5)
        return original(request)

    exchange.lp_snapshot = delayed
    slow = dict(condition_id='m00', token_id='m00')
    try:
        # Keep a real timeout against a definitely running blocked worker.
        lp._market_read_timeout = .02
        with pytest.raises(ValueError, match='market_read_timeout'):
            with _lp_read_stage('facts_read'):
                lp._read_snapshot(slow)
        assert 'lp_snapshot_stage stage=facts_read' in caplog.text
        caplog.clear()
        assert entered.is_set()
        lp._market_read_timeout = healthy_timeout
        with pytest.raises(ValueError, match='market_read_in_progress'):
            with _lp_read_stage('facts_read'):
                lp._read_snapshot(slow)
        assert 'reason=market_read_in_progress' in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert lp._read_snapshot(dict(condition_id='m01', token_id='m01'))['market']['token_id'] == 'm01'
        assert calls == ['m00']
    finally:
        release.set()
        lp._market_read_timeout = healthy_timeout
    # The worker's signal before returning is insufficient; await the real Future.
    futures[0].result(timeout=2)
    clock[0] = 159.999
    with pytest.raises(ValueError, match='market_read_cooling_down'):
        with _lp_read_stage('facts_read'):
            lp._read_snapshot(slow)
    assert 'reason=market_read_cooling_down' in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    clock[0] = 160.0
    assert lp._read_snapshot(slow)['market']['token_id'] == 'm00'
    assert calls == ['m00', 'm00'], 'late abandoned result must not become fresh facts'


def test_order_identity_mismatch_cannot_use_isolated_funds(tmp_path):
    execution, exchange, lp, _ = setup(tmp_path, 2)
    second = lp._candidate_pool.pop('m01')
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=2))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    lp._candidate_pool['m01'] = second
    read = exchange.lp_snapshot

    def conflict(request):
        snapshot = read(request)
        snapshot['orders'] = [{**order, 'token_id': 'wrong-token'} for order in snapshot['orders']]
        return snapshot

    exchange.lp_snapshot = conflict
    state = execution.lp_auto_run_once()
    assert len(exchange.posts) == 1
    assert 'unbounded_financial_uncertainty' in state['admission_block_reasons']
    assert state['funds']['spendable_usd'] is None


@pytest.mark.parametrize('status,reason,manual', [(401, 'credential_invalid', True), (403, 'external_snapshot_unknown', False)])
def test_review_account_http_classification_reaches_session(tmp_path, status, reason, manual):
    from datetime import datetime, UTC
    from urllib.error import HTTPError
    from tests.test_polymarket_lp import _SDKAccountClient, _SDKPublicClient, _request
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

    now = datetime.now(UTC)
    class Account(_SDKAccountClient):
        def list_open_orders(self):
            raise HTTPError('https://example.invalid', status, 'private response', {}, None)
    adapter = PolymarketTradingClient(TradingConfig('0x'+'1'*40, '0x'+'2'*40), Account(now), public_client_factory=lambda: _SDKPublicClient(now))
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, adapter, clock=lambda: now)
    store.lp_create_session('manual', 'manual', state='entry_open', payload={**_request(now), 'entry_order_id':'order-open', 'owned_order_ids':['order-open']})
    try:
        lp.reconcile_facts('manual', monitor=True)
        row = store.lp_session('manual')
        assert row['facts_error'] == reason
        assert row['manual_attention'] is manual
        assert 'private response' not in str(row)
    finally:
        adapter.close()


def test_review_account_retry_after_fixed_deadline(tmp_path, monkeypatch):
    from datetime import datetime, UTC, timedelta
    from email.message import Message
    from urllib.error import HTTPError
    from types import SimpleNamespace
    from tests.test_polymarket_lp import _SDKAccountClient, _SDKPublicClient, _request
    from open_trader import polymarket_trading
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

    now = datetime.now(UTC)
    clock = [1000.0]
    calls = []
    class Account(_SDKAccountClient):
        def list_open_orders(self):
            calls.append(clock[0])
            if len(calls) == 1:
                headers = Message(); headers['Retry-After'] = '120'
                raise HTTPError('https://example.invalid', 429, 'limited', headers, None)
            return super().list_open_orders()
    monkeypatch.setattr(polymarket_trading, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: now.timestamp()+clock[0]-1000))
    adapter = PolymarketTradingClient(TradingConfig('0x'+'1'*40, '0x'+'2'*40), Account(now), public_client_factory=lambda: _SDKPublicClient(now))
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, adapter, clock=lambda: now+timedelta(seconds=clock[0]-1000))
    store.lp_create_session('manual', 'manual', state='entry_open', payload={**_request(now), 'entry_order_id':'order-open', 'owned_order_ids':['order-open']})
    try:
        for elapsed in (0, 60, 119):
            clock[0] = 1000+elapsed
            lp.reconcile_facts('manual', monitor=True)
            row = store.lp_session('manual')
            assert row['facts_error'] == 'account_read_cooling_down'
            assert datetime.fromisoformat(row['reconcile_retry_at'].replace('Z','+00:00')) == now+timedelta(seconds=120)
            assert calls == [1000]
            with pytest.raises(ValueError, match='account_read_cooling_down'):
                adapter.lp_account_snapshot_shared()
            token = adapter.lp_account_round_begin()
            try:
                with pytest.raises(ValueError, match='account_read_cooling_down'):
                    adapter.lp_snapshot({**_request(now), '_lp_account_round': token})
            finally:
                adapter.lp_account_round_end(token)
            assert calls == [1000]
        clock[0] = 1120
        assert adapter.lp_snapshot(_request(now))['account']['authenticated'] is True
        assert calls == [1000, 1120]
    finally:
        adapter.close()


def test_review_capacity_is_visible_before_public_tick_can_read(tmp_path, monkeypatch):
    execution, exchange, lp, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    intent = execution.lp_auto_run_once()['intents'][0]
    published = Event()
    publish = store.lp_publish_facts
    def observed(*args, **kwargs):
        result = publish(*args, **kwargs)
        if kwargs.get('error') == 'facts_read_capacity':
            published.set()
        return result
    monkeypatch.setattr(store, 'lp_publish_facts', observed)
    lp._facts_capacity.acquire(); lp._facts_capacity.acquire()
    worker = Thread(target=execution.lp_tick)
    worker.start()
    try:
        assert published.wait(2)
        progress = execution._lp_session_progress(store.lp_session(intent['session_id']))
        assert progress['reason'] == 'facts_read_capacity'
        assert progress['retry_at'] is None
        row = execution._auto_pool._read()['intents'][intent['intent_id']]
        assert row['financial_status'] == 'unknown'
        assert Decimal(row['reserved_usd']) == Decimal(intent['reserved_usd'])
    finally:
        lp._facts_capacity.release(); lp._facts_capacity.release()
        worker.join(5)
    assert not worker.is_alive()
    assert store.lp_session(intent['session_id'])['facts_error'] is None


def test_review_stop_during_fault_send_does_not_announce_recovery(tmp_path):
    from datetime import datetime, UTC, timedelta
    from tests.test_polymarket_lp import _Exchange, _request
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    now = datetime.now(UTC)
    store = PredictionArbitrageStore(tmp_path)
    lp = PolymarketLPService(store, _Exchange(), clock=lambda: now)
    entered, release = Event(), Event()
    calls = []
    def notify(title, message, voice, *, channels):
        calls.append(title)
        if '已恢复' not in title:
            entered.set(); assert release.wait(5)
        return {key: True for key in channels}
    lp.set_protection_notifier(notify)
    store.lp_create_session('manual','manual',state='needs_attention',payload={**_request(now), 'submit_status':'unknown','facts_error':'trade_change_pending','reconciliation':'entry_submit_unknown','needs_attention_episode':'old','needs_attention_since':now-timedelta(seconds=301),'needs_attention_due':True})
    worker = Thread(target=lp.flush_session_attention,args=('manual',)); worker.start()
    try:
        assert entered.wait(2)
        assert lp.stop('manual')['state'] == 'review'
    finally:
        release.set(); worker.join(5)
    lp.flush_session_recovery('manual')
    assert len(calls) == 1
    row = store.lp_session('manual')
    assert row['submit_status'] == 'unknown'
    assert not row.get('needs_attention_recovery_due')


def test_review_good_monitor_apply_precedes_notification_schedule(tmp_path, monkeypatch):
    from datetime import timedelta
    from tests.test_lp_auto_pool import NOW
    execution, exchange, lp, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    intent = execution.lp_auto_run_once()['intents'][0]
    sid = intent['session_id']
    store.lp_update_session(sid, state='needs_attention', patch={
        'resume_state':'entry_open', 'needs_attention_episode':'old',
        'needs_attention_since':NOW-timedelta(seconds=301), 'needs_attention_due':True,
    })
    observed = []
    monkeypatch.setattr(lp, '_schedule_session_attention', lambda session_id: observed.append(store.lp_session(session_id)['state']))
    execution.lp_tick()
    assert observed
    assert 'needs_attention' not in observed


def test_review_late_fault_after_verified_recovery_notifies_once(tmp_path):
    from datetime import datetime, UTC, timedelta
    from tests.test_polymarket_lp import _Exchange, _request, _snapshot
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    now = datetime.now(UTC)
    store = PredictionArbitrageStore(tmp_path)
    exchange = _Exchange()
    request = _request(now)
    exchange.snapshot_value = {**_snapshot(now), 'orders': [{
        'order_id':'order-1', 'token_id':request['token_id'], 'side':'BUY',
        'status':'LIVE', 'price':request['price'], 'original_size':request['quantity'], 'size_matched':'0',
    }]}
    lp = PolymarketLPService(store, exchange, clock=lambda: now)
    entered, release = Event(), Event()
    calls = []
    def notify(title, message, voice, *, channels):
        calls.append(title)
        if '已恢复' not in title:
            entered.set(); assert release.wait(5)
        return {key: True for key in channels}
    lp.set_protection_notifier(notify)
    store.lp_create_session('manual','manual',state='needs_attention',payload={
        **request, 'entry_order_id':'order-1', 'owned_order_ids':['order-1'],
        'submit_status':'accepted','facts_error':'external_snapshot_unknown','resume_state':'entry_open',
        'needs_attention_episode':'old','needs_attention_since':now-timedelta(seconds=301),'needs_attention_due':True,
    })
    worker = Thread(target=lp.flush_session_attention,args=('manual',)); worker.start()
    try:
        assert entered.wait(2)
        lp.reconcile_facts('manual', monitor=True)
        row = store.lp_session('manual')
        assert row['position_reconciled'] is True and row['facts_error'] is None
    finally:
        release.set(); worker.join(5)
        delivery = lp._attention_thread
        if delivery is not None:
            delivery.join(5)
    lp.flush_session_recovery('manual')
    lp.flush_session_recovery('manual')
    assert len(calls) == 2 and '已恢复' in calls[1]


@pytest.mark.parametrize('wait_reason', ['facts_read_capacity', 'facts_read_in_progress'])
def test_manual_wait_progress_precedes_old_diagnostic_reason(tmp_path, wait_reason):
    from tests.test_lp_auto_pool import NOW, _manual_request

    execution, _, lp, store = setup(tmp_path)
    store.lp_create_session('manual-wait', 'manual-wait', state='needs_attention', payload={
        **_manual_request(NOW),
        'reconciliation': 'external_snapshot_unknown',
        'facts_error': 'external_snapshot_unknown',
        'resume_state': 'entry_open',
        'facts_checked_at': NOW,
        'reconcile_retry_at': NOW,
    })
    last_good = store.lp_session('manual-wait')['facts_checked_at']
    lp._publish_facts_wait('manual-wait', wait_reason)
    row = store.lp_session('manual-wait')
    progress = execution._lp_session_progress(row)
    assert progress['reason'] == wait_reason
    assert progress['retry_source'] == wait_reason
    assert progress['retry_at'] is None
    assert progress['next_action'] == 'read_only_reconcile'
    assert row['reconciliation'] == 'external_snapshot_unknown'
    assert row['facts_checked_at'] == last_good
    assert progress['last_good_check_at'] == row['facts_checked_at']
    assert not store.lp_auto_owns_session('manual-wait')


@pytest.mark.parametrize('path', ['/balance-allowance', '/data/orders', '/data/trades', '/positions'])
@pytest.mark.parametrize('status,retry_header', [(401, None), (429, '120'), (429, 'Wed, 30 Sep 2026 00:02:00 GMT')])
def test_real_sdk_account_response_facts_survive_transport(tmp_path, monkeypatch, path, status, retry_header):
    from datetime import datetime, UTC, timedelta
    from types import SimpleNamespace
    import httpx
    from eth_account import Account
    from polymarket import PRODUCTION, SecureClient
    from polymarket.models.clob import ApiKeyCreds
    from open_trader import polymarket_trading
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig

    now = datetime(2026, 9, 30, tzinfo=UTC)
    clock = [1000.0]
    calls, seen = [], []
    failed = [False]
    # Synthetic signer/credentials only; construction performs no network I/O.
    signer = Account.from_key('0x' + '11' * 32)
    sdk = SecureClient._construct_for_wallet(
        signer=signer, wallet=signer.address, environment=PRODUCTION,
        credentials=ApiKeyCreds(key='test-key', secret='c3ludGhldGlj', passphrase='test-passphrase'),
        api_key=None, logger=None,
    )
    def response(request):
        calls.append(request.url.path)
        if request.url.path == path and not failed[0]:
            failed[0] = True
            headers = {'X-Private-Header': 'header-secret', 'Date': 'Wed, 30 Sep 2026 00:00:00 GMT'}
            if retry_header is not None:
                headers['Retry-After'] = retry_header
            return httpx.Response(status, headers=headers, json={'error':'body-secret'}, request=request)
        payload = (
            {'balance':'100000000', 'allowances':{str(PRODUCTION.standard_exchange):'100000000'}}
            if request.url.path == '/balance-allowance'
            else [] if request.url.path == '/positions'
            else {'data':[], 'next_cursor':'LTE='}
        )
        return httpx.Response(200, json=payload, request=request)
    def existing_hook(response):
        seen.append(response.status_code)
    transports = [sdk._ctx.secure_clob, sdk._ctx.data]
    for transport in transports:
        base_url = str(transport._client.base_url)
        transport._client.close()
        transport._client = httpx.Client(base_url=base_url, transport=httpx.MockTransport(response), event_hooks={'response':[existing_hook]})
    monkeypatch.setattr(polymarket_trading, 'time', SimpleNamespace(monotonic=lambda:clock[0], time=lambda:now.timestamp()+clock[0]-1000))
    adapter = PolymarketTradingClient(TradingConfig(signer.address, signer.address), sdk, public_client_factory=lambda:None)
    request = {'market_id':'market', 'condition_id':'condition', 'token_id':'token'}
    reason = 'credential_invalid' if status == 401 else 'account_read_cooling_down'
    def hooks_unchanged():
        assert all(t._client.event_hooks['response'] == [existing_hook] for t in transports)
        assert sdk._ctx.clob._client.event_hooks['response'] == []
    try:
        with pytest.raises(ValueError, match=reason) as error:
            adapter.lp_account_snapshot_shared()
        assert 'header-secret' not in repr(vars(error.value)) + str(error.value)
        assert 'body-secret' not in repr(vars(error.value)) + str(error.value)
        hooks_unchanged()
        count = len(calls)
        assert calls.count(path) == 1 and seen[-1] == status
        if status == 429:
            assert error.value.retry_at == now+timedelta(seconds=120)
            for elapsed in (60,119):
                clock[0] = 1000+elapsed
                with pytest.raises(ValueError, match=reason) as retry:
                    adapter.lp_snapshot(request)
                assert retry.value.retry_at == now+timedelta(seconds=120)
                with pytest.raises(ValueError, match=reason):
                    adapter.lp_account_snapshot_shared()
                token = adapter.lp_account_round_begin()
                try:
                    with pytest.raises(ValueError, match=reason):
                        adapter.lp_snapshot({**request, '_lp_account_round':token})
                finally:
                    adapter.lp_account_round_end(token)
                assert len(calls) == count
                hooks_unchanged()
            clock[0] = 1120
        assert adapter.lp_account_snapshot_shared()['authenticated'] is True
        assert calls.count(path) == 2
        hooks_unchanged()
    finally:
        adapter.close()
        sdk.close()


@pytest.mark.parametrize('conflict', ['revision', 'generation'])
def test_store_publish_conflict_logs_wait_without_applying_facts(tmp_path, caplog, conflict):
    import logging
    from open_trader.polymarket_trading import _lp_read_stage
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    caplog.set_level(logging.INFO, logger="open_trader.polymarket_trading")
    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session('s', 's', state='entry_open', payload={'owned_order_ids': ['order'], 'reserved_usd': '8'})
    before = store.lp_session('s')
    reason = 'session_changed' if conflict == 'revision' else 'account_round_invalid'
    with pytest.raises(ValueError, match=reason):
        with _lp_read_stage('facts_publish'):
            store.lp_publish_facts('s', 1 if conflict == 'revision' else 0,
                                   trade_generation=1 if conflict == 'generation' else None,
                                   patch={'reserved_usd': '0'}, state='complete')
    assert store.lp_session('s') == before
    assert store.lp_actions('s') == []
    assert f'reason={reason}' in caplog.text
    assert 'lp_read_wait stage=facts_publish' in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize('reason', ['session_changed', 'account_round_invalid', 'market_read_capacity', 'market_read_in_progress', 'market_read_cooling_down', 'market_closed'])
def test_untyped_same_message_is_still_a_fault(reason, caplog):
    from open_trader.polymarket_trading import _lp_read_stage
    with pytest.raises(ValueError, match=reason):
        with _lp_read_stage('facts_read'):
            raise ValueError(reason)
    assert 'lp_snapshot_stage stage=facts_read' in caplog.text
    assert 'lp_read_wait' not in caplog.text


def test_unlisted_typed_wait_reason_remains_fault(caplog):
    from open_trader.polymarket_lp_errors import LpObservationWait
    from open_trader.polymarket_trading import _lp_read_stage
    with pytest.raises(LpObservationWait):
        with _lp_read_stage('market'):
            raise LpObservationWait('market_read_timeout')
    assert 'lp_snapshot_stage stage=market' in caplog.text
    assert 'lp_read_wait' not in caplog.text
