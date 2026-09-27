"""Exercise the real #195 producer and #197 consumer with a simulated venue."""

from decimal import Decimal
import json
import threading

import pytest

from open_trader.polymarket_lp_reports import AutoDailyReports
from tests import test_lp_auto_pool as pool
from tests.test_lp_auto_reports import moment
from tests.test_dashboard_web import run_dashboard_js
from tests.test_prediction_service import _ProductionRuntime, _production_server, _response


def test_produced_auto_facts_drive_daily_cutoffs_without_reconciling_on_read(tmp_path, monkeypatch):
    engine, exchange, lp, store = pool.setup(tmp_path)
    engine.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1})
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_run_once(round_id="report-integration")
    assert len(exchange.posts) == 1
    exchange.orders[0].update(status="FILLED", size_matched="20")
    exchange.positions = [dict(token_id="m00", condition_id="m00", size="20")]
    exchange.orders.append(dict(order_id="manual", token_id="manual-token", condition_id="manual-market",
                                side="BUY", status="LIVE", price=".4", original_size="100", size_matched="0"))
    exchange.trades = [
        dict(trade_id=trade, status="CONFIRMED", matched_at=at,
             maker_orders=[dict(order_id="o1", token_id="m00", side="BUY", matched_amount=quantity, price=".40", fee="0")])
        for trade, at, quantity in [
            ("before", "2026-09-27T15:59:59Z", "12"),
            ("boundary", "2026-09-27T16:00:00Z", "8"),
        ]
    ]
    monkeypatch.setattr(pool, "NOW", moment("2026-09-27T16:05:00Z"))
    engine.lp_auto_reconcile_unknown()
    engine.lp_auto_reconcile_unknown()  # Repeated venue observations remain one fact.
    state = engine.lp_auto_state()
    def forbidden(*args, **kwargs):
        pytest.fail("report query/generation must not read the venue or submit orders")
    monkeypatch.setattr(exchange, "lp_snapshot", forbidden)
    monkeypatch.setattr(exchange, "lp_account_snapshot", forbidden)
    monkeypatch.setattr(exchange, "lp_post_order", forbidden)
    reporter = AutoDailyReports(store, engine.lp_auto_report_facts, engine.lp_auto_state, now=lambda: pool.NOW)
    saved = reporter.generate_due()[0]
    assert saved["report_date"] == "2026-09-27"
    assert saved["metrics"]["intent_count"] == 1
    assert saved["metrics"]["accepted_count"] == 1
    assert saved["metrics"]["filled_order_count"] == 1
    assert Decimal(saved["metrics"]["filled_quantity"]) == 12
    assert saved["financial_period"]["status"] == "known"
    assert Decimal(saved["financial_period"]["inventory_cost_usd"]) == Decimal("4.80")
    assert Decimal(saved["financial_period"]["inventory_quantity"]) == 12
    assert Decimal(saved["funds"]["inventory_cost_usd"]) == 8  # Latest state is deliberately different.
    assert saved["closing_orders"][0]["remaining_quantity"] == "8"
    today = reporter.today()
    assert today["metrics"]["intent_count"] == 0
    assert Decimal(today["metrics"]["filled_quantity"]) == 8
    assert today["closing_orders"] == []
    assert not any(row.get("order_id") == "manual" for row in saved["events"])
    assert reporter.generate_due() == []
    assert reporter.history()["reports"][0]["report_date"] == saved["report_date"]
    assert engine.lp_auto_state() == state
    assert len(exchange.posts) == 1
    midnight = "2026-09-27T16:00:00Z"
    empty = engine.lp_auto_report_facts(period_start=midnight, period_end=midnight)
    assert empty["financial_period"]["status"] == "known"
    assert Decimal(empty["financial_period"]["realized_pnl_usd"]) == 0

    # Inject only the clocked reporter; HTTP handlers and execution readers stay real.
    monkeypatch.setattr(engine, "_lp_auto_reports", lambda: reporter)
    runtime = _ProductionRuntime()
    runtime.execution = engine
    with _production_server(runtime) as (base, _):
        status, history = _response(base + "/api/prediction-arbitrage/lp/auto/reports")
        assert status == 200 and history["today"]["metrics"]["filled_quantity"] == "8"
        assert "events" not in history["reports"][0]
        status, detail = _response(base + "/api/prediction-arbitrage/lp/auto/reports/2026-09-27")
        assert status == 200 and detail == saved
        status, dashboard = _response(base + "/api/prediction-arbitrage/lp/dashboard")
        assert status == 200 and dashboard["auto_summary"] == history
    output = run_dashboard_js(
        "state.predictionMarket.lpAutoReport = " + json.dumps({
            "account_id": saved["account_id"], "auto_run_id": saved["auto_run_id"],
            "report_date": saved["report_date"], "data": detail,
        }) + "; console.log(predictionLpDailySummary(" + json.dumps(dashboard["auto_summary"]) + "));"
    )
    assert "成交数量 <strong>8</strong>" in output and "成交数量 <strong>12</strong>" in output
    assert "期末库存成本 $4.80" in output and "库存占用 $8.00" in output
    assert "manual-token" not in output
    assert engine.lp_auto_state() == state and len(exchange.posts) == 1


def test_real_reconciliation_block_does_not_delay_paused_auto_daily_report(tmp_path, monkeypatch):
    from open_trader import prediction_runtime
    engine, exchange, lp, store = pool.setup(tmp_path)
    engine.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1})
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_run_once(round_id="clock-integration")
    engine.lp_auto_set_desired_running(False)
    monkeypatch.setattr(pool, "NOW", moment("2026-09-27T16:05:00Z"))
    reporter = AutoDailyReports(store, engine.lp_auto_report_facts, engine.lp_auto_state, now=lambda: pool.NOW)
    monkeypatch.setattr(engine, "_lp_auto_reports", lambda: reporter)
    blocked, release, generated = threading.Event(), threading.Event(), threading.Event()
    def snapshot(*args, **kwargs):
        blocked.set()
        release.wait(5)
        raise RuntimeError("simulated venue unavailable")
    generate = engine.lp_generate_due_auto_reports
    def generate_and_signal():
        generate()
        generated.set()
    monkeypatch.setattr(exchange, "lp_snapshot", snapshot)
    monkeypatch.setattr(engine, "lp_generate_due_auto_reports", generate_and_signal)
    monkeypatch.setattr(prediction_runtime, "_LP_TICK_SECONDS", 0.01)
    runtime = prediction_runtime.PredictionRuntime(
        data_dir=tmp_path, prediction_config_path=tmp_path / "config.json", dashboard_url="http://127.0.0.1")
    runtime.execution, runtime.lp = engine, lp
    try:
        runtime._start_lp_monitor()
        assert blocked.wait(2)
        runtime._start_lp_daily_report_monitor()
        assert generated.wait(2) and not release.is_set()
        saved = reporter.report("2026-09-27")
        assert saved["runtime"]["desired_running"] is False
        assert saved["metrics"]["accepted_count"] == 1
        assert engine.lp_auto_state()["desired_running"] is False and len(exchange.posts) == 1
        assert store.lp_daily_report("2026-09-27") is None
    finally:
        runtime._lp_stop_event.set()
        runtime._lp_report_stop_event.set()
        release.set()
        if runtime._lp_thread:
            runtime._lp_thread.join(2)
        if runtime._lp_report_thread:
            runtime._lp_report_thread.join(2)
