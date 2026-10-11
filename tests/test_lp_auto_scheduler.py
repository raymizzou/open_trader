from datetime import UTC, datetime, timedelta
import threading

from open_trader.polymarket_lp_scheduler import LPAutoScheduler


def test_immediate_minute_checks_cross_midnight_and_retry_after_failure():
    now = datetime(2026, 9, 27, 15, 59, 30, tzinfo=UTC)
    calls = []

    class Execution:
        def lp_auto_run_once(self):
            calls.append(now)
            if len(calls) == 2:
                raise RuntimeError("temporary account read failure")

    scheduler = LPAutoScheduler(Execution(), clock=lambda: now)
    assert scheduler.run_due()
    assert not scheduler.run_due()
    now += timedelta(seconds=60)
    assert scheduler.run_due()
    assert scheduler.snapshot()["last_check_error"] == "RuntimeError"
    now += timedelta(seconds=60)
    assert scheduler.run_due()
    assert scheduler.snapshot()["last_check_error"] is None
    assert len(calls) == 3
    scheduler.request_check()
    assert not scheduler.run_due()
    now += timedelta(seconds=60)
    assert scheduler.run_due()
    assert len(calls) == 4


def test_cycles_do_not_overlap_and_wakeup_during_cycle_is_not_lost():
    now = datetime(2026, 9, 27, 8, tzinfo=UTC)
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Execution:
        def lp_auto_run_once(self):
            calls.append(1)
            entered.set()
            assert release.wait(2)

    scheduler = LPAutoScheduler(Execution(), clock=lambda: now)
    worker = threading.Thread(target=scheduler.run_due)
    worker.start()
    try:
        assert entered.wait(2)
        assert scheduler.snapshot()["check_in_progress"] is True
        scheduler.request_check()
        assert not scheduler.run_due()
    finally:
        release.set()
        worker.join(2)
    assert not scheduler.run_due()
    assert len(calls) == 1
    now += timedelta(seconds=60)
    assert scheduler.run_due()
    assert len(calls) == 2


def test_monitor_start_and_stop_do_not_change_manual_intent():
    checked = threading.Event()

    class Execution:
        def lp_auto_run_once(self):
            checked.set()

    scheduler = LPAutoScheduler(Execution())
    scheduler.start()
    try:
        assert checked.wait(2)
        assert scheduler.snapshot()["scheduler_running"] is True
    finally:
        scheduler.stop()
    assert scheduler.snapshot()["scheduler_running"] is False
