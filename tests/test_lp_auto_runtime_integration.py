"""#196 runs the actual #195 pool, SQLite state and LP order lifecycle."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
import threading
import sqlite3

import pytest

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_lp_scheduler import LPAutoScheduler
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests import test_lp_auto_pool as pool


def restart(tmp_path, exchange, old_lp):
    store = PredictionArbitrageStore(tmp_path / "state.sqlite")
    lp = PolymarketLPService(store, exchange, clock=lambda: pool.NOW)
    lp._candidate_pool = deepcopy(old_lp._candidate_pool)
    lp._candidate_qualification_facts = deepcopy(old_lp._candidate_qualification_facts)
    execution = PredictionExecutionService(
        store=store, monitor=SimpleNamespace(), trading=exchange,
        notifier=SimpleNamespace(), lock_path=tmp_path / "execution.lock", lp=lp,
    )
    execution._breaker_open = False  # Simulated clean startup; venue reads remain real core gates.
    return execution, lp


def refresh_candidates(lp, exchange):
    for condition in lp._candidate_qualification_facts:
        lp._candidate_pool_record_success(
            condition, {"condition_id": condition}, judged_at=pool.NOW,
            facts={"directions": [exchange.direction(condition)],
                   "account": exchange.lp_account_snapshot()},
        )


@pytest.mark.parametrize("paused", [False, True])
def test_restart_reconciles_before_refill_and_preserves_manual_intent(tmp_path, monkeypatch, paused):
    execution, exchange, lp, store = pool.setup(tmp_path, 2)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 2})
    # Startup isolates a failed read of the first order while its new sibling
    # can use fresh account facts and the remaining conservative allocation.
    second = lp._candidate_qualification_facts.pop("m01")
    execution.lp_auto_set_desired_running(True)
    scheduler = LPAutoScheduler(execution, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    assert len(exchange.posts) == 1
    if paused:
        execution.lp_auto_set_desired_running(False)
    lp._candidate_qualification_facts["m01"] = second
    restored, restored_lp = restart(tmp_path, exchange, lp)
    read_snapshot = exchange.lp_snapshot

    def unavailable(request):
        raise OSError("account unavailable after restart")

    exchange.lp_snapshot = unavailable
    after_restart = LPAutoScheduler(restored, clock=lambda: pool.NOW)
    assert after_restart.run_due()
    assert len(exchange.posts) == (1 if paused else 2)
    assert restored.lp_auto_state()["desired_running"] is (not paused)
    assert restored.lp_auto_state()["block_reasons"]
    exchange.lp_snapshot = read_snapshot
    monkeypatch.setattr(pool, "NOW", pool.NOW + timedelta(seconds=60))
    refresh_candidates(restored_lp, exchange)
    assert after_restart.run_due()
    assert len(exchange.posts) == (1 if paused else 2)
    assert restored.lp_auto_state()["run_id"] == execution.lp_auto_state()["run_id"]
    if paused:
        restored.lp_auto_set_desired_running(True)
        after_restart.request_check()
        assert after_restart.run_due()
        assert len(exchange.posts) == 2


def test_pause_during_reconciliation_survives_late_result(tmp_path):
    execution, exchange, lp, store = pool.setup(tmp_path, 2)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 2})
    second = lp._candidate_qualification_facts.pop("m01")
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    lp._candidate_qualification_facts["m01"] = second
    entered, release = threading.Event(), threading.Event()
    read_snapshot = exchange.lp_snapshot

    def delayed(request):
        entered.set()
        assert release.wait(3)
        return read_snapshot(request)

    exchange.lp_snapshot = delayed
    scheduler = LPAutoScheduler(execution, clock=lambda: pool.NOW)
    worker = threading.Thread(target=scheduler.run_due)
    worker.start()
    try:
        assert entered.wait(3)
        assert execution.lp_auto_set_desired_running(False)["pause_confirmed"] is True
        with pytest.raises(ValueError):
            execution.lp_auto_configure({"budget_usd": "0", "target_buy_count": 0})
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert len(exchange.posts) == 1
    assert execution.lp_auto_state()["desired_running"] is False


def test_midnight_gtd_termination_refills_from_fresh_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "NOW", datetime(2026, 9, 27, 15, 59, 30, tzinfo=UTC))
    execution, exchange, lp, store = pool.setup(tmp_path, 2)
    scheduler = LPAutoScheduler(execution, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    execution.lp_auto_configure({"budget_usd": "8", "target_buy_count": 1})
    assert not exchange.posts
    execution.lp_auto_set_desired_running(True)
    scheduler.request_check()
    assert scheduler.run_due()
    assert len(exchange.posts) == 1
    identity = execution.lp_auto_state()["run_id"]
    exchange.orders[0]["status"] = "EXPIRED"
    monkeypatch.setattr(pool, "NOW", pool.NOW + timedelta(seconds=60))
    # The next pool has a different eligible market; no old queue is replayed.
    lp._candidate_qualification_facts.pop("m00")
    refresh_candidates(lp, exchange)
    execution.lp_tick()
    assert scheduler.run_due()
    assert len(exchange.posts) == 2
    assert exchange.posts[-1]["token_id"] == "m01"
    state = execution.lp_auto_state()
    assert state["run_id"] == identity
    assert state["desired_running"] is True
    assert state["slots"]["occupied"] == 1


def test_terminal_pause_confirmation_blocks_unsigned_remainder_and_keeps_buy(tmp_path, capsys):
    from open_trader import cli
    from tests.test_lp_auto_control import runtime_for
    from tests.test_prediction_service import _server

    execution, exchange, lp, store = pool.setup(tmp_path, 2)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 2})
    second = lp._candidate_qualification_facts.pop("m01")
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    lp._candidate_qualification_facts["m01"] = second
    entered, release = threading.Event(), threading.Event()

    def before_sign():
        entered.set()
        assert release.wait(3)

    exchange.before_sign = before_sign
    scheduler = LPAutoScheduler(execution, clock=lambda: pool.NOW)
    runtime = runtime_for(tmp_path)
    runtime.execution = execution
    runtime._lp_auto_scheduler = scheduler
    with _server(runtime) as base:
        worker = threading.Thread(target=scheduler.run_due)
        worker.start()
        try:
            assert entered.wait(3)
            assert cli.main(["prediction-arb", "lp-auto", "pause", "--url", base]) == 0
            assert "PAUSED" in capsys.readouterr().out
        finally:
            release.set()
            worker.join(3)
    assert not worker.is_alive()
    assert len(exchange.posts) == 1
    assert exchange.orders[0]["status"] == "LIVE"
    assert execution.lp_auto_state()["desired_running"] is False


@pytest.mark.parametrize("pause_while_blocked", [False, True])
def test_durable_receipt_reconciles_after_interrupted_submit_without_overriding_pause(tmp_path, monkeypatch, pause_while_blocked):
    execution, exchange, lp, store = pool.setup(tmp_path, 2)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 2})
    execution.lp_auto_set_desired_running(True)
    update = store.lp_update_session

    def fail_after_receipt(session_id, **kwargs):
        if (kwargs.get("patch") or {}).get("submit_status") == "accepted":
            raise OSError("interrupted after durable accepted action")
        return update(session_id, **kwargs)

    monkeypatch.setattr(store, "lp_update_session", fail_after_receipt)
    scheduler = LPAutoScheduler(execution, clock=lambda: pool.NOW)
    scheduler.run_due()
    assert len(exchange.posts) == 1
    assert scheduler.snapshot()["last_check_error"] == "OSError"
    restored, restored_lp = restart(tmp_path, exchange, lp)
    read_snapshot = exchange.lp_snapshot
    exchange.lp_snapshot = lambda request: (_ for _ in ()).throw(OSError("read unavailable"))
    recovered = LPAutoScheduler(restored, clock=lambda: pool.NOW)
    recovered.run_due()
    assert len(exchange.posts) == 2
    assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01']
    assert restored.lp_auto_state()["block_reasons"]
    if pause_while_blocked:
        assert restored.lp_auto_set_desired_running(False)["pause_confirmed"] is True
    exchange.lp_snapshot = read_snapshot
    monkeypatch.setattr(pool, "NOW", pool.NOW + timedelta(seconds=60))
    refresh_candidates(restored_lp, exchange)
    recovered.run_due()
    assert not restored.lp_auto_state()["block_reasons"]
    assert len(exchange.posts) == 2  # no duplicate after receipt recovery or pause
    assert restored.lp_auto_state()["desired_running"] is (not pause_while_blocked)


def test_shadow_auto_http_reads_do_not_initialize_or_write_durable_state(tmp_path):
    from tests.test_lp_auto_control import runtime_for
    from tests.test_prediction_service import _server, _response

    execution, exchange, lp, store = pool.setup(tmp_path)
    runtime = runtime_for(tmp_path)
    runtime._mode = "shadow"
    runtime.execution, runtime.lp, runtime.store = execution, lp, store
    runtime._lp_auto_scheduler = None
    with sqlite3.connect(f"file:{store.path}?mode=ro", uri=True) as connection:
        version = connection.execute("PRAGMA data_version").fetchone()
        with _server(runtime) as base:
            for suffix in ("auto/state", "dashboard", "auto/state"):
                status, payload = _response(base + "/api/prediction-arbitrage/lp/" + suffix)
                assert status == 200
        assert connection.execute("PRAGMA data_version").fetchone() == version
        assert connection.execute("SELECT COUNT(*) FROM lp_auto_pool").fetchone()[0] == 0
    assert not exchange.posts


def test_paused_inventory_exit_continues_and_confirmed_proceeds_fund_next_buy(tmp_path, monkeypatch):
    execution, exchange, lp, store = pool.setup(tmp_path, 2)
    execution.lp_auto_configure({"budget_usd": "8", "target_buy_count": 1})
    execution.lp_auto_set_desired_running(True)
    scheduler = LPAutoScheduler(execution, clock=lambda: pool.NOW)
    scheduler.run_due()
    exchange.orders[0].update(status="FILLED", size_matched="20")
    exchange.positions = [{"token_id": "m00", "condition_id": "m00", "size": "20"}]
    execution.lp_auto_set_desired_running(False)
    execution.lp_tick()
    assert len(exchange.posts) == 2
    assert exchange.posts[-1]["side"] == "SELL"
    scheduler.request_check()
    scheduler.run_due()
    assert Decimal(execution.lp_auto_state()["funds"]["inventory_cost_usd"]) == 8
    assert Decimal(execution.lp_auto_state()["funds"]["available_usd"]) == 0
    execution.lp_auto_set_desired_running(True)
    scheduler.request_check()
    scheduler.run_due()
    assert len(exchange.posts) == 2
    exchange.orders[-1].update(status="FILLED", size_matched="20")
    exchange.positions = []
    monkeypatch.setattr(pool, "NOW", pool.NOW + timedelta(seconds=60))
    lp._candidate_qualification_facts.pop("m00")
    refresh_candidates(lp, exchange)
    execution.lp_tick()
    scheduler.run_due()
    assert len(exchange.posts) == 3
    assert exchange.posts[-1]["side"] == "BUY"
    assert exchange.posts[-1]["token_id"] == "m01"
    state = execution.lp_auto_state()
    assert Decimal(state["funds"]["realized_pnl_usd"]) == Decimal("0.20")
    assert Decimal(state["funds"]["total_usd"]) == Decimal("8")
    assert Decimal(state["funds"]["available_usd"]) == Decimal("0")


def test_existing_lp_card_keeps_real_auto_trading_controls_without_daily_summary(tmp_path):
    import json
    from tests.test_dashboard_web import run_dashboard_js

    execution, exchange, lp, store = pool.setup(tmp_path)
    execution.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1})
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    auto = execution.lp_auto_state()
    dashboard = {"auto": auto, "auto_summary": execution.lp_auto_report()}
    html = run_dashboard_js(
        "console.log(predictionLpCard({lp_dashboard:" + json.dumps(dashboard) + "}));"
    )
    assert 'data-lp-auto-action="pause"' in html
    assert "策略总资金" in html and "BUY 总预留" in html
    assert auto["intents"][0]["intent_id"] not in html
    assert auto["intents"][0]["order_id"] not in html
    assert "自动订单 / 意图" not in html
    assert "自然日汇总" not in html
