"""The operator pause must not do report work or remove trading controls."""
from types import SimpleNamespace

from open_trader.prediction_runtime import PredictionRuntime
from tests import test_lp_auto_pool as pool
from tests.test_dashboard_web import run_dashboard_js
from tests.test_prediction_service import _ProductionRuntime, _production_server, _response


def forbidden(*args, **kwargs):
    raise AssertionError("paused reporting performed work")


def test_paused_reporting_has_no_worker_or_execution_reads(tmp_path, monkeypatch):
    runtime = PredictionRuntime(data_dir=tmp_path, prediction_config_path=tmp_path / "config.json",
                                dashboard_url="http://127.0.0.1")
    runtime.execution = SimpleNamespace(lp_generate_due_auto_reports=forbidden)
    runtime._start_lp_daily_report_monitor()
    try:
        assert runtime._lp_report_thread is None
    finally:
        runtime._lp_report_stop_event.set()
        if runtime._lp_report_thread:
            runtime._lp_report_thread.join(2)
    engine, exchange, lp, store = pool.setup(tmp_path)
    monkeypatch.setattr(engine, "_lp_auto_reports", forbidden)
    monkeypatch.setattr(engine._lp_auto_pool(), "reconcile_reports", forbidden)
    assert engine.lp_generate_due_auto_reports() == []
    assert engine.lp_auto_report()["state"] == "paused"
    assert engine.lp_auto_summary()["state"] == "paused"
    engine.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1})
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_run_once()
    assert len(exchange.posts) == 1
    assert engine.lp_auto_state()["desired_running"] is True


def test_paused_http_never_reads_report_and_dashboard_keeps_trading():
    runtime = _ProductionRuntime()
    runtime.execution = SimpleNamespace(lp_auto_report=forbidden, lp_dashboard=lambda: {"state": "ready"})
    with _production_server(runtime) as (base, _):
        for suffix in ("", "/2026-09-28"):
            status, body = _response(base + "/api/prediction-arbitrage/lp/auto/reports" + suffix)
            assert status == 200 and body["state"] == "paused"
        status, body = _response(base + "/api/prediction-arbitrage/lp/dashboard")
        assert status == 200 and body["state"] == "ready"
        assert "auto_summary" not in body


def test_lp_card_does_not_render_paused_panels_even_with_old_payload():
    html = run_dashboard_js('''
predictionLpDailySummary = () => {throw Error("report rendered");};
predictionLpPreparation = () => {throw Error("preparation rendered");};
console.log(predictionLpCard({lp_dashboard:{auto_summary:{today:{}},preparation:{state:"ready"}}}));
''')
    assert "自然日汇总" not in html and "LP 准备状态" not in html
    assert "当天 LP 委托" in html and 'data-action="lp-dashboard-refresh"' in html
