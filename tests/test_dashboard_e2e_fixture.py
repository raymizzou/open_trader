"""Offline contracts for the concurrent browser fixture server."""
from http.cookiejar import CookieJar
import importlib.util
import json
from pathlib import Path
import threading
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

import pytest


@pytest.fixture(autouse=True)
def unexpected_system_proxy(monkeypatch):
    # Force proxy routing unless each local client explicitly bypasses it.
    monkeypatch.setattr("urllib.request.getproxies", lambda: {"http": "http://127.0.0.1:1"})
    monkeypatch.setattr("urllib.request.proxy_bypass", lambda _host: False)
    monkeypatch.setattr("urllib.request._opener", None)


def test_browser_fixture_contexts_keep_independent_scenarios_and_mutations() -> None:
    spec = importlib.util.spec_from_file_location(
        "dashboard_e2e_fixture", Path(__file__).parent / "e2e/serve_dashboard_fixture.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.FixtureServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    first, second = (
        build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        for _ in range(2)
    )

    def get(client, path):
        with client.open(base + path, timeout=5) as response:
            return response.read()

    try:
        get(first, "/?prediction_state=ready")
        get(second, "/?prediction_state=empty")
        expected = module._prediction_payload("empty")
        assert expected != module._prediction_payload("ready"), "initial scenarios must differ observably"
        assert json.loads(get(first, "/api/prediction-arbitrage/state")) == module._prediction_payload("ready")
        assert json.loads(get(second, "/api/prediction-arbitrage/state")) == expected
        with first.open(Request(base + "/api/prediction-arbitrage/executions", data=b"{}"), timeout=5) as response:
            assert response.status == 200
        assert json.loads(get(first, "/api/prediction-arbitrage/state")) == module._prediction_payload("success")
        assert json.loads(get(second, "/api/prediction-arbitrage/state")) == expected
        # Navigating one context cannot reset another context's mutation state.
        get(second, "/?prediction_state=observation-fetch-error")
        assert json.loads(get(first, "/api/prediction-arbitrage/state")) == module._prediction_payload("success")
        for _ in range(3):
            assert json.loads(get(second, "/api/prediction-arbitrage/state")) == module._prediction_payload("observation-fetch-error")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_browser_fixture_processes_report_distinct_owned_dynamic_listeners() -> None:
    import select
    import subprocess
    import sys

    client = build_opener(ProxyHandler({}))

    script = Path(__file__).parent / "e2e/serve_dashboard_fixture.py"
    processes = []
    urls = []
    try:
        for _ in range(2):
            process = subprocess.Popen(
                [sys.executable, str(script), "--port", "0"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            processes.append(process)
            ready, _, _ = select.select([process.stdout], [], [], 5)
            assert ready, "fixture did not publish its bound listener"
            line = process.stdout.readline().strip()
            assert line.startswith("fixture_dashboard_url: http://127.0.0.1:"), line
            url = line.removeprefix("fixture_dashboard_url: ")
            assert not url.endswith(":0")
            urls.append(url)
            with client.open(url, timeout=5) as response:
                assert response.status == 200
            assert process.poll() is None
        assert len(set(urls)) == 2
    finally:
        for process in processes:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
            assert process.poll() is not None
