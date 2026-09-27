from datetime import UTC, datetime
from decimal import Decimal

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


def moment(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def facts(events=(), **state):
    return {
        "account_id": "account-a", "auto_run_id": "auto-1",
        "state": {"enabled_at": "2026-09-25T17:00:00Z", "desired_running": False,
                  "runtime_state": "needs_attention", "reason": "unknown_submit", **state},
        "events": list(events), "sessions": [],
        "funds": {"status": "unknown", "available_usd": None, "as_of": "2026-09-26T16:05:00Z"},
        "financial_period": {"status": "unknown", "realized_pnl_usd": None, "inventory_cost_usd": None},
        "as_of": "2026-09-26T16:05:00Z",
    }


def event(key, kind, at, **values):
    return {"event_id": key, "kind": kind, "occurred_at": at,
            "observed_at": at or "2026-09-26T16:03:00Z", "auto_run_id": "auto-1",
            "account_id": "account-a", "intent_id": "i1", "order_id": "o1", **values}


def test_event_days_deduplicate_and_keep_undated_unknown_intents_visible():
    from open_trader.polymarket_lp_reports import build_auto_report
    fill = event("f1", "fill", "2026-09-26T15:59:59Z", quantity="3", side="BUY")
    events = [
        event("i1", "intent", "2026-09-25T23:00:00Z", order_id=None),
        event("a1", "accepted", "2026-09-25T23:00:01Z", quantity="10"),
        fill, fill,
        event("c1", "cancel", "2026-09-26T15:59:59Z"),
        event("i2", "intent", "2026-09-26T15:00:00Z", intent_id="i2", order_id=None),
        event("u2", "unknown", None, intent_id="i2", order_id=None),
        event("manual", "intent", "2026-09-26T14:00:00Z", auto_run_id=None),
        event("boundary", "fill", "2026-09-26T16:00:00Z", quantity="7"),
    ]
    report = build_auto_report(facts(events), "2026-09-26", now=moment("2026-09-26T16:05:00Z"), frozen=True)
    assert report["period_start"] == "2026-09-25T16:00:00Z"
    assert report["period_end"] == "2026-09-26T16:00:00Z"
    assert report["metrics"] == {"intent_count": 2, "accepted_count": 1, "rejected_count": 0,
                                 "unknown_count": 1, "filled_order_count": 1,
                                 "filled_quantity": "3", "cancelled_order_count": 1}
    assert any(row["intent_id"] == "i2" and row["order_id"] is None for row in report["pending"])
    assert report["financial_period"]["realized_pnl_usd"] is None
    assert report["funds"]["scope"] == "latest_observation_not_period_end"
    assert report["coverage"]["actual_start"] == "2026-09-25T17:00:00Z"
    assert report["closing_orders_status"] == "unknown"


def test_same_timestamp_terminal_action_wins_over_acceptance():
    from open_trader.polymarket_lp_reports import build_auto_report
    source = facts([event("z-accept", "accepted", "2026-09-26T15:59:59Z", quantity="10"),
                    event("a-cancel", "cancel", "2026-09-26T15:59:59Z")])
    report = build_auto_report(source, "2026-09-26", now=moment("2026-09-26T16:05:00Z"), frozen=True)
    assert report["closing_orders"] == []
    source["events"][1]["status"] = "EXPIRED"
    expired = build_auto_report(source, "2026-09-26", now=moment("2026-09-26T16:05:00Z"), frozen=True)
    assert expired["closing_orders"] == []
    assert expired["metrics"]["cancelled_order_count"] == 0


def test_restart_backfill_retains_cutoff_unknown_when_acceptance_happens_next_day(tmp_path):
    from open_trader.polymarket_lp_reports import AutoDailyReports
    source = facts([event("i", "intent", "2026-09-26T15:59:00Z", order_id=None),
                    event("u", "unknown", "2026-09-26T15:59:01Z", order_id=None),
                    event("a", "accepted", "2026-09-26T16:01:00Z", quantity="10")])
    reporter = AutoDailyReports(PredictionArbitrageStore(tmp_path), lambda **_: source, lambda: source["state"],
                               now=lambda: moment("2026-09-27T01:00:00Z"))
    report = reporter.generate_due()[0]
    assert report["metrics"]["unknown_count"] == 1
    assert report["metrics"]["accepted_count"] == 0
    assert report["closing_orders_status"] == "unknown"
    assert any(row["kind"] == "unknown" for row in report["events"])
    assert any(row["intent_id"] == "i1" for row in report["pending"])
    today = reporter.today()
    assert today["metrics"]["accepted_count"] == 1
    assert today["metrics"]["unknown_count"] == 0


def test_new_day_unknown_submission_is_not_a_previous_day_pending_order():
    from open_trader.polymarket_lp_reports import build_auto_report
    source = facts([event("new-intent", "intent", "2026-09-26T16:01:00Z", order_id=None),
                    event("new-unknown", "unknown", None, order_id=None)])
    report = build_auto_report(source, "2026-09-26", now=moment("2026-09-26T16:05:00Z"), frozen=True)
    assert report["pending"] == []
    assert report["metrics"]["intent_count"] == 0


def test_receipt_uncertainty_after_acceptance_stays_pending_until_explicit_recovery(tmp_path):
    from open_trader.polymarket_lp_reports import AutoDailyReports
    clock = [moment("2026-09-26T16:05:00Z")]
    missing = event("receipt_unknown:o1", "unknown", None, side="SELL",
                    reason="order_receipt_unknown", observed_at="2026-09-26T15:59:00Z")
    source = facts([event("i", "intent", "2026-09-26T15:00:00Z", side="SELL"),
                    event("a", "accepted", "2026-09-26T15:00:01Z", side="SELL", quantity="10"), missing])
    reports = AutoDailyReports(PredictionArbitrageStore(tmp_path), lambda **_: source,
                               lambda: source["state"], now=lambda: clock[0])
    saved = reports.generate_due()[0]
    assert saved["metrics"]["accepted_count"] == 1
    assert saved["metrics"]["unknown_count"] == 0  # Acceptance is known; current order state is not.
    assert saved["closing_orders_status"] == "unknown"
    assert saved["closing_orders"][0]["state"] == "unknown"
    assert any(row["event_id"] == missing["event_id"] for row in saved["pending"])
    assert reports.today()["closing_orders_status"] == "unknown"
    missing["resolved_at"] = "2026-09-26T16:06:00Z"
    assert reports.today()["closing_orders_status"] == "unknown"  # Future recovery is not evidence yet.
    clock[0] = moment("2026-09-26T16:06:00Z")
    recovered = reports.today()
    assert recovered["pending"] == []
    assert recovered["closing_orders_status"] == "from_saved_events"
    assert recovered["metrics"]["accepted_count"] == 0
    assert recovered["closing_orders"][0]["remaining_quantity"] == "10"
    assert reports.report("2026-09-26") == saved


def test_cross_day_stocks_and_late_ack_are_not_new_daily_orders():
    from open_trader.polymarket_lp_reports import build_auto_report
    events = [event("i", "intent", "2026-09-25T15:00:00Z"),
              event("a", "accepted", "2026-09-25T15:00:01Z", quantity="10", observed_at="2026-09-26T16:02:00Z"),
              event("f", "fill", "2026-09-26T15:59:59Z", quantity="2"),
              event("cr", "cancel_requested", "2026-09-26T15:59:59Z"),
              event("c", "cancel", "2026-09-26T16:01:00Z")]
    report = build_auto_report(facts(events), "2026-09-26", now=moment("2026-09-26T16:05:00Z"), frozen=True)
    assert report["metrics"]["intent_count"] == 0
    assert report["metrics"]["accepted_count"] == 0
    assert report["metrics"]["cancelled_order_count"] == 0
    assert report["closing_orders"][0]["state"] == "cancel_pending"
    assert report["closing_orders"][0]["remaining_quantity"] == "8"
    assert report["closing_orders"][0]["carried_in"] is True
    assert report["late_events"][0]["event_id"] == "a"


def test_zero_activity_rewards_unknown_and_day_boundary_live_range():
    from open_trader.polymarket_lp_reports import build_auto_report
    report = build_auto_report(facts(), "2026-09-27", now=moment("2026-09-26T16:00:00Z"))
    assert report["period_start"] == report["period_end"] == "2026-09-26T16:00:00Z"
    assert report["metrics"]["intent_count"] == 0
    assert report["runtime"]["desired_running"] is False
    assert report["rewards"]["automatic_total_usd"] is None
    assert report["rewards"]["paid"] is False
    delayed = build_auto_report(facts(), "2026-09-26", now=moment("2026-09-26T16:05:30Z"), frozen=True)
    assert delayed["late_generated"] is True
    assert delayed["generation_delay_seconds"] == 30


def test_late_resolution_replaces_unknown_without_duplicating_action_or_rewriting_saved_day(tmp_path):
    from open_trader.polymarket_lp_reports import AutoDailyReports
    clock = [moment("2026-09-26T16:05:00Z")]
    source = facts([
        event("i", "intent", "2026-09-26T14:00:00Z", order_id=None),
        event("u", "unknown", None, order_id=None),
    ])
    reports = AutoDailyReports(PredictionArbitrageStore(tmp_path), lambda **_: source, lambda: source["state"], now=lambda: clock[0])
    original = reports.generate_due()[0]
    clock[0] = moment("2026-09-26T16:10:00Z")
    ack = event("a", "accepted", "2026-09-26T14:00:01Z", observed_at="2026-09-26T16:10:00Z", quantity="10")
    source["events"].extend([ack, ack])
    assert reports.report("2026-09-26") == original
    today = reports.today()
    assert today["metrics"]["accepted_count"] == 0
    assert today["closing_orders"][0]["carried_in"] is True
    assert today["pending"] == []
    assert today["late_events"][0]["event_id"] == "a"


def test_cross_midnight_partial_fills_keep_shared_financial_projection_and_reward_scope():
    from open_trader.polymarket_lp_reports import build_auto_report
    source = facts([
        event("i", "intent", "2026-09-26T15:00:00Z"),
        event("a", "accepted", "2026-09-26T15:00:01Z", quantity="10"),
        event("f1", "fill", "2026-09-26T15:59:59Z", quantity="3"),
        event("f2", "fill", "2026-09-26T16:00:00Z", quantity="2"),
    ])
    source["financial_period"] = {"status": "known", "realized_pnl_usd": "1.23", "inventory_cost_usd": "1.50", "inventory_quantity": "3", "source": "shared_verified_trades"}
    reward = {"condition_id": "market", "reward_date": "2026-09-26", "market_amount": "10", "status": "known", "checked_at": "2026-09-26T16:03:00Z"}
    source["sessions"] = [{"reward_observation": reward}, {"reward_observation": reward}]
    report = build_auto_report(source, "2026-09-26", now=moment("2026-09-26T16:05:00Z"), frozen=True)
    assert report["metrics"]["filled_quantity"] == "3"
    assert report["closing_orders"][0]["remaining_quantity"] == "7"
    assert report["financial_period"] == source["financial_period"]
    assert len(report["rewards"]["observations"]) == 1
    assert report["rewards"]["automatic_total_usd"] is None
    assert report["rewards"]["included_in_reusable_funds"] is False


def test_due_clock_backfills_all_missing_days_freezes_and_never_mutates_control(tmp_path):
    from open_trader.polymarket_lp_reports import AutoDailyReports
    store = PredictionArbitrageStore(tmp_path)
    current = [moment("2026-09-26T16:04:59Z")]
    saved_facts = facts()
    read_calls = []
    def read(**kwargs):
        read_calls.append(kwargs)
        return saved_facts
    reports = AutoDailyReports(store, read, lambda: saved_facts["state"], now=lambda: current[0])
    assert reports.generate_due() == []
    current[0] = moment("2026-09-26T16:05:00Z")
    first = reports.generate_due()
    assert [r["report_date"] for r in first] == ["2026-09-26"]
    assert first[0]["late_generated"] is False
    assert reports.generate_due() == []
    current[0] = moment("2026-09-29T01:00:00Z")
    restarted = AutoDailyReports(store, read, lambda: saved_facts["state"], now=lambda: current[0])
    assert [r["report_date"] for r in restarted.generate_due()] == ["2026-09-27", "2026-09-28"]
    assert all(r["late_generated"] for r in restarted.history()["reports"][:2])
    assert restarted.report("2026-09-26") == first[0]
    assert saved_facts["state"]["desired_running"] is False


def test_natural_reports_are_immutable_and_scoped_by_account_and_auto_identity(tmp_path):
    store = PredictionArbitrageStore(tmp_path)
    payload = {"generated_at": "2026-09-26T16:05:00Z", "quantity": Decimal("2.5")}
    saved = store.lp_save_auto_daily_report("account-a", "auto-1", "2026-09-26", payload)
    replay = store.lp_save_auto_daily_report(
        "account-a", "auto-1", "2026-09-26", {**payload, "quantity": "99"}
    )
    assert replay == saved
    assert Decimal(saved["quantity"]) == Decimal("2.5")
    assert store.lp_auto_daily_report("account-a", "auto-2", "2026-09-26") is None
    assert store.lp_auto_daily_report("account-b", "auto-1", "2026-09-26") is None
    assert store.lp_daily_report("2026-09-26") is None  # 08:00 report is separate.
    for account, auto, day in [("account-b", "auto-1", "2026-09-26"), ("account-a", "auto-1", "2026-09-27")]:
        store.lp_save_auto_daily_report(account, auto, day, payload)
    assert [r["report_date"] for r in store.lp_auto_daily_reports("account-a", "auto-1")] == ["2026-09-27", "2026-09-26"]
    assert PredictionArbitrageStore(tmp_path).lp_auto_daily_report("account-a", "auto-1", "2026-09-26") == saved
    with pytest.raises(ValueError):
        store.lp_save_auto_daily_report("account-a", "auto-1", "2026-09-99", payload)
    with pytest.raises(ValueError):
        store.lp_save_auto_daily_report("", "auto-1", "2026-09-26", payload)


def test_fact_read_failure_still_freezes_unknown_report(tmp_path):
    from open_trader.polymarket_lp_reports import AutoDailyReports
    state = {**facts()["state"], "account_id": "account-a", "auto_run_id": "auto-1"}
    def broken(**_):
        raise RuntimeError("offline")
    reports = AutoDailyReports(PredictionArbitrageStore(tmp_path), broken, lambda: state,
                              now=lambda: moment("2026-09-26T16:05:00Z"))
    report = reports.generate_due()[0]
    assert report["coverage"]["gaps"] == ["enabled_after_period_start", "facts_read_failed"]
    assert report["metrics"]["intent_count"] is None
    assert report["closing_orders_status"] == "unknown"
    assert reports.history()["today"]["metrics"]["filled_quantity"] is None


def test_report_thread_is_independent_of_a_blocked_reconciliation(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from open_trader import prediction_runtime
    from open_trader.polymarket_lp_reports import AutoDailyReports
    from open_trader.prediction_runtime import PredictionRuntime
    blocked, release, generated = threading.Event(), threading.Event(), threading.Event()
    def tick():
        blocked.set()
        release.wait(3)
        raise RuntimeError("account read failed")
    store = PredictionArbitrageStore(tmp_path)
    reporter = AutoDailyReports(store, lambda **_: facts(), lambda: facts()["state"],
                               now=lambda: moment("2026-09-26T16:05:00Z"))
    def report():
        reporter.generate_due()
        generated.set()
    runtime = PredictionRuntime(data_dir=tmp_path, prediction_config_path=tmp_path / "config.json",
                                dashboard_url="http://127.0.0.1")
    runtime.lp = SimpleNamespace()
    runtime.execution = SimpleNamespace(lp_tick=tick, lp_generate_due_auto_reports=report)
    monkeypatch.setattr(prediction_runtime, "_LP_TICK_SECONDS", 0.01)
    try:
        runtime._start_lp_monitor()
        assert blocked.wait(2)
        runtime._start_lp_daily_report_monitor()
        assert generated.wait(2)
        thread = runtime._lp_report_thread
        runtime._start_lp_daily_report_monitor()
        assert runtime._lp_report_thread is thread
        assert store.lp_auto_daily_report("account-a", "auto-1", "2026-09-26") is not None
        assert not release.is_set()
    finally:
        runtime._lp_stop_event.set()
        runtime._lp_report_stop_event.set()
        release.set()
        runtime._lp_thread.join(2)
        runtime._lp_report_thread.join(2)


def test_auto_report_http_and_dashboard_are_read_only_and_keep_unknowns(tmp_path):
    from types import SimpleNamespace
    from tests.test_prediction_service import _ProductionRuntime, _production_server, _response
    from open_trader.polymarket_lp_reports import AutoDailyReports
    reporter = AutoDailyReports(PredictionArbitrageStore(tmp_path), lambda **_: facts(), lambda: facts()["state"],
                               now=lambda: moment("2026-09-26T16:05:00Z"))
    reporter.generate_due()
    runtime = _ProductionRuntime()
    runtime.execution = SimpleNamespace(
        lp_auto_report=lambda day=None: reporter.report(day) if day else reporter.history(),
        lp_dashboard=lambda: {"state": "snapshot_pending"},
    )
    with _production_server(runtime) as (base, _):
        status, body = _response(base + "/api/prediction-arbitrage/lp/auto/reports")
        assert status == 200 and body["today"]["funds"]["available_usd"] is None
        assert len(body["reports"]) == 1
        assert "events" not in body["reports"][0]  # Five-second polling reads metadata only.
        assert _response(base + "/api/prediction-arbitrage/lp/auto/reports/2026-09-26")[0] == 200
        assert _response(base + "/api/prediction-arbitrage/lp/auto/reports/2026-09-25")[0] == 404
        assert _response(base + "/api/prediction-arbitrage/lp/auto/reports/2026-09-99")[0] == 400
        status, body = _response(base + "/api/prediction-arbitrage/lp/dashboard")
        assert status == 200 and body["auto_summary"]["today"]["runtime"]["desired_running"] is False


def test_natural_day_panel_renders_unknown_pending_identity_and_history():
    import json
    from tests.test_dashboard_web import run_dashboard_js
    from open_trader.polymarket_lp_reports import build_auto_report
    source = facts([event("u", "unknown", None, intent_id="<script>x</script>", order_id=None)])
    report = build_auto_report(source, "2026-09-26", now=moment("2026-09-26T16:05:00Z"), frozen=True)
    output = run_dashboard_js("console.log(predictionLpDailySummary(" + json.dumps({"today": report, "reports": [report]}) + "));")
    assert "北京时间今天截至现在" in output and "2026-09-26" in output
    assert "无可靠订单 ID" in output and "&lt;script&gt;" in output
    assert "<script>x</script>" not in output
    assert "本日已核实交易盈亏 UNKNOWN" in output
    assert "每天 00:05 保存前一天" in output
    assert "累计不代表到账" in output
    assert "期末剩余挂单 未知 · 撤单中 未知" in output


def test_history_loads_one_day_read_only_and_ignores_an_older_selection():
    import json
    from tests.test_dashboard_web import run_dashboard_js
    output = run_dashboard_js(r'''
const scope = {account_id: "account-a", auto_run_id: "auto-1"};
state.predictionMarket.lpDashboard = {auto_summary: {today: scope}};
const calls = [], responses = [];
globalThis.fetch = (url, options) => {calls.push({url, options}); return new Promise(resolve => responses.push(resolve));};
const first = loadLpAutoReport("2026-09-25");
const second = loadLpAutoReport("2026-09-26");
responses[1]({ok: true, json: async () => ({...scope, report_date: "2026-09-26"})});
await second;
responses[0]({ok: true, json: async () => ({...scope, report_date: "2026-09-25"})});
await first;
console.log(JSON.stringify({calls, selected: state.predictionMarket.lpAutoReport.data.report_date}));
''')
    result = json.loads(output)
    assert result["selected"] == "2026-09-26"
    assert len(result["calls"]) == 2
    assert all(row["options"].get("method", "GET") == "GET" for row in result["calls"])
    assert result["calls"][0]["url"].endswith("/auto/reports/2026-09-25")
