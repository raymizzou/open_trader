import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from types import SimpleNamespace

import pytest

from open_trader import cli
from open_trader.polymarket_lp_scheduler import LPAutoScheduler
from open_trader.prediction_runtime import PredictionRuntime
from tests.test_prediction_service import _server, _response, _production_request


class ControlExecution:
    def __init__(self):
        self.state = {"desired_running": False, "pause_confirmed": True}
        self.calls = []

    def lp_auto_state(self, *, include_intents=True):
        return dict(self.state)

    def lp_auto_set_desired_running(self, running, **kwargs):
        self.calls.append(running)
        self.state.update(desired_running=running, pause_confirmed=not running)
        return self.lp_auto_state()

    def lp_auto_configure(self, payload, **kwargs):
        self.calls.append(payload)
        if self.state["desired_running"]:
            raise ValueError("manual_pause_required")
        self.state.update(payload)
        return self.lp_auto_state()


def runtime_for(tmp_path):
    runtime = PredictionRuntime(data_dir=tmp_path, prediction_config_path=tmp_path / "config.json", dashboard_url="http://localhost", n_leg_paused=True)
    runtime._state = "RUNNING"
    runtime._owner = SimpleNamespace(held=True)
    runtime.execution = ControlExecution()
    runtime._lp_auto_scheduler = LPAutoScheduler(runtime.execution)
    return runtime


def test_dashboard_omits_intents_without_materializing_detail_projection(tmp_path, monkeypatch):
    from open_trader import polymarket_lp_auto
    from tests.test_lp_auto_pool import setup

    runtime = runtime_for(tmp_path)
    engine, exchange, lp, store = setup(tmp_path)
    runtime.execution = engine
    engine.lp_auto_configure({"budget_usd": "100", "target_buy_count": 1})
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_run_once()
    before = engine._lp_auto_pool()._read()
    expected = runtime.lp_auto_state()
    assert expected.pop("intents") and len(exchange.posts) == 1
    monkeypatch.setattr(engine, "lp_dashboard", lambda: {"state": "ready"})
    copy = polymarket_lp_auto.deepcopy

    def reject_intent_copy(value):
        if isinstance(value, list) and any("intent_id" in row for row in value):
            raise AssertionError("paused intent detail projection performed work")
        return copy(value)

    with _server(runtime) as base:
        assert _response(base + "/api/prediction-arbitrage/lp/auto/state")[1]["intents"]
        monkeypatch.setattr(polymarket_lp_auto, "deepcopy", reject_intent_copy)
        status, body = _response(base + "/api/prediction-arbitrage/lp/dashboard")
    assert status == 200 and body["auto"] == expected
    assert engine._lp_auto_pool()._read() == before
    assert len(exchange.posts) == 1


def test_auto_api_controls_schema_auth_and_confirmation(tmp_path):
    runtime = runtime_for(tmp_path)
    root = "/api/prediction-arbitrage/lp/auto"
    with _server(runtime, session_token="session-token", csrf_token="csrf-token") as base:
        status, state = _response(base + root + "/state")
        assert status == 200 and state["desired_running"] is False
        for action in ("enable", "pause", "resume"):
            assert _response(_production_request(base, root + "/" + action, b'{"confirm":true}', headers={"X-CSRF-Token": "invalid"}))[0] == 403
            assert _response(_production_request(base, root + "/" + action, b'{"confirm":false}'))[0] == 400
        assert runtime.execution.calls == []
        config = {"budget_usd": "100", "target_buy_count": 3, "expected_config_version": 0}
        assert _response(_production_request(base, root + "/config", json.dumps(config).encode()))[0] == 200
        assert runtime.execution.state["desired_running"] is False
        for action, running in (("enable", True), ("pause", False), ("resume", True)):
            status, state = _response(_production_request(base, root + "/" + action, b'{"confirm":true}'))
            assert status == 200 and state["desired_running"] is running
            if not running:
                assert state["pause_confirmed"] is True
        assert _response(_production_request(base, root + "/config", json.dumps(config).encode()))[0] == 400
        runtime._owner.held = False
        assert _response(base + root + "/state")[0] == 503
        assert _response(_production_request(base, root + "/pause", b'{"confirm":true}'))[0] == 503


def test_cli_pause_waits_for_service_confirmation_and_fails_when_unreachable(tmp_path, capsys):
    runtime = runtime_for(tmp_path)
    runtime.execution.state["desired_running"] = True
    with _server(runtime) as base:
        assert cli.main(["prediction-arb", "lp-auto", "pause", "--url", base]) == 0
        output = capsys.readouterr().out
        assert "PAUSED" in output
        assert runtime.execution.state["desired_running"] is False
    assert cli.main(["prediction-arb", "lp-auto", "pause", "--url", base, "--timeout", "0.2"]) == 2
    output = capsys.readouterr().out
    assert "PAUSED" not in output and "UNKNOWN" in output


def test_unconfirmed_pause_is_never_acknowledged(tmp_path, capsys):
    runtime = runtime_for(tmp_path)
    runtime.execution.lp_auto_set_desired_running = lambda running, **kwargs: {"desired_running": False, "pause_confirmed": False}
    with _server(runtime) as base:
        assert cli.main(["prediction-arb", "lp-auto", "pause", "--url", base]) == 2
        assert "PAUSED" not in capsys.readouterr().out


@pytest.mark.parametrize("url", ["https://127.0.0.1:8769", "http://example.com", "http://user@127.0.0.1:8769", "http://127.0.0.1:8769/path"])
def test_cli_rejects_nonlocal_or_ambiguous_control_url(url, capsys):
    assert cli.main(["prediction-arb", "lp-auto", "pause", "--url", url]) == 2
    assert "PAUSED" not in capsys.readouterr().out


def test_shutdown_keeps_execution_and_owner_while_auto_check_is_in_flight(tmp_path):
    runtime = runtime_for(tmp_path)
    execution = runtime.execution

    def pending():
        raise RuntimeError("pending exchange request")

    runtime._lp_auto_scheduler.stop = pending
    assert runtime._cleanup_resources()
    assert runtime.execution is execution
    assert runtime.production_owner


def test_auto_ui_keeps_configuration_draft_and_checks_pause_confirmation():
    from tests.test_dashboard_web import run_dashboard_js

    result = json.loads(run_dashboard_js(r'''
state.predictionMarket.csrfToken = "csrf";
const auto = {desired_running: false, ever_enabled: false, pause_confirmed: true, budget_usd: "100", target_buy_count: 3, config_version: 7, slots: {occupied: 0}, block_reasons: []};
state.predictionMarket.lpDashboard = {auto};
const before = predictionLpAutoControls(auto, false);
handleLpAutoInput({target: {closest: () => ({dataset: {lpAutoField: "budget_usd"}, value: "120"})}});
const draft = predictionLpAutoControls({...auto, budget_usd: "999", config_version: 8}, false);
const running = predictionLpAutoControls({...auto, desired_running: true}, false);
const calls = [];
renderPredictionMarket = () => {};
fetchPredictionLpDashboard = async () => {};
predictionPost = async (path, body) => { calls.push({path, body}); return {desired_running: false, pause_confirmed: false}; };
await controlLpAuto("pause");
const unconfirmed = state.predictionMarket.lpAutoMessage;
predictionPost = async (path, body) => { calls.push({path, body}); return auto; };
await controlLpAuto("config");
console.log(JSON.stringify({before, draft, running, unconfirmed, calls}));
'''))
    assert "启用自动补位" in result["before"]
    assert 'value="120"' in result["draft"]
    assert "暂停新增" in result["running"]
    assert "操作未确认" in result["unconfirmed"]
    assert result["calls"][1]["body"] == {"budget_usd": "120", "target_buy_count": 3, "expected_config_version": 7}


def test_old_dashboard_poll_cannot_overwrite_confirmed_manual_pause():
    from tests.test_dashboard_web import run_dashboard_js

    result = json.loads(run_dashboard_js(r'''
state.workspaceView = "prediction_market";
state.predictionMarket.activeTab = "lp";
state.predictionMarket.csrfToken = "csrf";
renderPredictionMarket = () => {};
predictionRequestUrl = path => path;
let finishOld;
fetch = () => new Promise(resolve => { finishOld = resolve; });
const oldPoll = fetchPredictionLpDashboard();
predictionPost = async () => ({desired_running: false, pause_confirmed: true});
await controlLpAuto("pause");
finishOld({ok: true, json: async () => ({auto: {desired_running: true, pause_confirmed: false}})});
await oldPoll;
console.log(JSON.stringify(state.predictionMarket.lpDashboard.auto));
'''))
    assert result["desired_running"] is False
    assert result["pause_confirmed"] is True


@pytest.mark.parametrize("redirect_status", [301, 302, 303, 307, 308])
def test_cli_does_not_follow_redirected_pause_or_accept_its_confirmation(redirect_status, capsys):
    forwarded = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.endswith("/venues"):
                body = b'{"csrf_token":"secret"}'
            else:
                forwarded.append(dict(self.headers))
                body = b'{"desired_running":false,"pause_confirmed":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.send_response(redirect_status)
            self.send_header("Location", "/fake-confirmation")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert cli.main(["prediction-arb", "lp-auto", "pause", "--url", base]) == 2
        assert "PAUSED" not in capsys.readouterr().out
        assert forwarded == []
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_ui_distinguishes_saved_intent_from_scheduler_health():
    from tests.test_dashboard_web import run_dashboard_js

    result = run_dashboard_js(r'''
state.predictionMarket.csrfToken = "csrf";
console.log(predictionLpAutoControls({desired_running: true, scheduler_running: false, slots: {occupied: 0}}, false));
''')
    assert "调度未运行" in result
    assert "人工意愿：运行" in result
    assert 'data-lp-auto-action="pause">暂停新增' in result
    assert "持续运行" not in result


def test_pause_can_persist_when_scheduler_is_unavailable(tmp_path):
    runtime = runtime_for(tmp_path)
    runtime._lp_auto_scheduler = None
    runtime.execution.state["desired_running"] = True
    result = runtime.lp_auto_set_desired_running(False)
    assert result["pause_confirmed"] is True
    assert result["scheduler_running"] is False
    with pytest.raises(RuntimeError):
        runtime.lp_auto_set_desired_running(True)


def test_cli_uses_one_deadline_for_bootstrap_and_pause(monkeypatch, capsys):
    from io import BytesIO

    clock = [100.0]
    calls = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request, timeout))
            if len(calls) == 1:
                clock[0] += 0.15  # bootstrap consumes half of the shared budget
                return BytesIO(b'{"csrf_token":"test-token"}')
            assert request.method == "POST"
            # The pause needs another 250ms; only 150ms must remain.
            assert timeout == pytest.approx(0.15)
            raise TimeoutError("pause exceeded the remaining shared deadline")

    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(cli, "build_opener", lambda *args: Opener())
    assert cli.main(["prediction-arb", "lp-auto", "pause", "--url", "http://127.0.0.1:8769", "--timeout", "0.3"]) == 2
    output = capsys.readouterr().out
    assert "UNKNOWN" in output and "PAUSED" not in output
    assert len(calls) == 2
    assert calls[0][1] == pytest.approx(0.3)


def test_cli_real_deadline_expires_during_unconfirmed_pause(tmp_path, monkeypatch, capsys):
    from concurrent.futures import ThreadPoolExecutor
    from open_trader import prediction_service

    runtime = runtime_for(tmp_path)
    bootstrap_done, pause_entered, release_pause = (threading.Event() for _ in range(3))

    def bootstrap(**kwargs):
        bootstrap_done.set()
        return {"csrf_token": kwargs["csrf_token"], "venues": []}

    pause = runtime.execution.lp_auto_set_desired_running

    def blocked_pause(running, **kwargs):
        pause_entered.set()
        assert release_pause.wait(3)
        return pause(running, **kwargs)

    monkeypatch.setattr(prediction_service, "prediction_venues_payload", bootstrap)
    runtime.execution.lp_auto_set_desired_running = blocked_pause
    with _server(runtime) as base, ThreadPoolExecutor(1) as workers:
        result = workers.submit(cli.main, ["prediction-arb", "lp-auto", "pause", "--url", base, "--timeout", "0.3"])
        try:
            assert bootstrap_done.wait(2)
            assert pause_entered.wait(2), "real timeout must exercise the pause response"
            assert result.result(timeout=2) == 2
            output = capsys.readouterr().out
            assert "UNKNOWN" in output and "PAUSED" not in output
            assert not release_pause.is_set()
        finally:
            release_pause.set()


def test_cli_loopback_pause_does_not_use_environment_proxy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.delenv("NO_PROXY", raising=False)
    runtime = runtime_for(tmp_path)
    with _server(runtime) as base:
        assert cli.main(["prediction-arb", "lp-auto", "pause", "--url", base]) == 0
    assert "PAUSED" in capsys.readouterr().out
