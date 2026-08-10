from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from typing import Iterator
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

from open_trader.prediction_read_model import (
    prediction_history_payload,
    prediction_state_payload,
)
from open_trader.prediction_service import create_prediction_server
from tests.test_prediction_read_model import (
    _CrossVenueMonitor,
    _Execution,
    _Monitor,
    _Store,
)


FROZEN_PREDICTION_MUTATION_PATHS = (
    "/api/prediction-arbitrage/preview",
    "/api/prediction-arbitrage/executions",
    "/api/prediction-arbitrage/mode",
    "/api/prediction-arbitrage/circuit-breaker/reset",
    "/api/prediction-arbitrage/predict-allowance/cleanup",
    "/api/prediction-arbitrage/cross-auto/pause",
)


class _Runtime:
    def __init__(self, *, state: str = "RUNNING", violation: dict[str, object] | None = None) -> None:
        self.state = state
        self.store = _Store()
        self.monitor = _Monitor()
        self.execution = _Execution()
        self.cross_venue_monitor = _CrossVenueMonitor()
        self.shadow_evidence = {
            "mode": "shadow",
            "guard_attempts": [] if violation is None else [violation],
            "first_violation": violation,
            "codex": {
                "relation": {"calls": 1, "successes": 1},
                "cross_venue": {"calls": 2, "successes": 1},
            },
        }
        self.polls = 0

    def poll_shadow_failure(self) -> dict[str, object] | None:
        self.polls += 1
        return self.shadow_evidence["first_violation"]  # type: ignore[return-value]


@contextmanager
def _server(runtime: _Runtime) -> Iterator[str]:
    server = create_prediction_server(runtime=runtime, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _response(request: str | Request) -> tuple[int, dict[str, object]]:
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_shadow_health_has_the_read_only_identity() -> None:
    with _server(_Runtime()) as base:
        status, payload = _response(base + "/healthz")

    assert status == 200
    assert payload["schema_version"] == "open_trader.prediction_service.health.v1"
    assert payload["module"] == "prediction_service"
    assert payload["status"] == "running"
    assert payload["mode"] == "shadow"
    assert payload["production_owner"] is False
    assert payload["mutations"] == "prohibited"
    assert payload["runtime_state"] == "RUNNING"
    assert payload["codex"] == {
        "relation": {"calls": 1, "successes": 1},
        "cross_venue": {"calls": 2, "successes": 1},
    }
    assert payload["first_violation"] is None
    assert payload["guard_attempts"] == []
    assert isinstance(payload["pid"], int)
    assert isinstance(payload["started_at"], str)


def test_shadow_state_and_history_use_the_shared_read_model() -> None:
    runtime = _Runtime()
    expected_state = prediction_state_payload(
        store=runtime.store,
        monitor=runtime.monitor,
        execution=runtime.execution,
        csrf_token="",
        cross_venue_monitor=runtime.cross_venue_monitor,
    )
    expected_history = prediction_history_payload(
        runtime.store,
        kind="signals",
        limit=1,
        offset=0,
        monitor=runtime.monitor,
        execution=runtime.execution,
        cross_venue_monitor=runtime.cross_venue_monitor,
    )
    with _server(runtime) as base:
        state_status, state = _response(base + "/api/prediction-arbitrage/state")
        history_status, history = _response(
            base + "/api/prediction-arbitrage/history?kind=signals&limit=1&offset=0"
        )

    assert state_status == history_status == 200
    assert state == expected_state
    assert history == expected_history


def test_shadow_history_rejects_invalid_query() -> None:
    with _server(_Runtime()) as base:
        status, payload = _response(
            base + "/api/prediction-arbitrage/history?kind=signals&limit=0"
        )

    assert status == 400
    assert payload == {"error": "limit must be positive"}


@pytest.mark.parametrize("path", FROZEN_PREDICTION_MUTATION_PATHS)
def test_shadow_rejects_every_mutation_before_dispatch(path: str) -> None:
    runtime = _Runtime()
    with _server(runtime) as base:
        request = Request(
            base + path,
            data=b'{"unexpected":"payload"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        status, payload = _response(request)

    assert status == 403
    assert payload == {
        "code": "shadow_read_only",
        "message": "Shadow Prediction Service is read-only",
    }


def test_shadow_health_and_reads_fail_closed_when_runtime_is_not_running() -> None:
    with _server(_Runtime(state="FAILED")) as base:
        health_status, health = _response(base + "/healthz")
        state_status, state = _response(base + "/api/prediction-arbitrage/state")

    assert health_status == state_status == 503
    assert health["status"] == "unavailable"
    assert state == {"error": "shadow runtime is unavailable"}


def test_shadow_health_and_reads_fail_closed_after_a_violation() -> None:
    violation = {"venue": "predict", "kind": "mutation", "method": "submit_order", "call_chain": []}
    with _server(_Runtime(violation=violation)) as base:
        health_status, health = _response(base + "/healthz")
        state_status, state = _response(base + "/api/prediction-arbitrage/state")

    assert health_status == state_status == 503
    assert health["first_violation"] == violation
    assert health["guard_attempts"] == [violation]
    assert state == {"error": "shadow runtime is unavailable"}


def test_shadow_service_rejects_non_loopback_before_binding() -> None:
    with pytest.raises(ValueError, match="loopback"):
        create_prediction_server(runtime=_Runtime(), host="0.0.0.0", port=0)


@pytest.mark.parametrize("method", ("HEAD", "PUT", "DELETE", "OPTIONS"))
def test_unsupported_http_methods_return_not_found(method: str) -> None:
    with _server(_Runtime()) as base:
        status = _status(Request(base + "/unsupported", method=method))

    assert status == 404


def _status(request: Request) -> int:
    try:
        with urlopen(request, timeout=5) as response:
            return response.status
    except HTTPError as error:
        return error.code


def test_shadow_mutations_do_not_read_body_or_dispatch_downstream() -> None:
    runtime = _Runtime()
    probes = []

    class Probe:
        def __init__(self) -> None:
            self.calls: list[str] = []
            probes.append(self)

        def __getattr__(self, name: str) -> object:
            self.calls.append(name)
            raise AssertionError(f"unexpected downstream access: {name}")

    runtime.store = Probe()
    runtime.monitor = Probe()
    runtime.execution = Probe()
    runtime.cross_venue_monitor = Probe()
    runtime.session = Probe()
    runtime.csrf = Probe()
    with _server(runtime) as base:
        parsed = urlsplit(base)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as connection:
            connection.sendall(
                (
                    "POST /api/prediction-arbitrage/preview HTTP/1.1\r\n"
                    f"Host: {parsed.netloc}\r\n"
                    "Content-Length: 999999\r\n"
                    "Content-Type: application/json\r\n\r\n"
                ).encode("ascii")
            )
            response = connection.recv(1024)

    assert b"403" in response.split(b"\r\n", 1)[0]
    assert all(probe.calls == [] for probe in probes)


def test_owner_loop_keeps_failed_shadow_listener_for_observability(tmp_path: Path) -> None:
    trigger = tmp_path / "violate"
    stopped = tmp_path / "stopped"
    violation = {"venue": "predict", "kind": "mutation", "method": "submit_order", "call_chain": []}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    script = f'''\
from pathlib import Path
import open_trader.prediction_service as service

trigger = Path({str(trigger)!r})
stopped = Path({str(stopped)!r})
violation = {{"venue": "predict", "kind": "mutation", "method": "submit_order", "call_chain": []}}

class FakeRuntime:
    def __init__(self, **_kwargs):
        self.state = "NEW"
        self.shadow_evidence = {{"mode": "shadow", "first_violation": None, "codex": {{}}}}
    def start(self):
        self.state = "RUNNING"
    def poll_shadow_failure(self):
        if trigger.exists():
            self.shadow_evidence["first_violation"] = violation
            return violation
        return None
    def stop(self):
        self.state = "STOPPED"
        stopped.write_text("stopped", encoding="utf-8")

service.PredictionRuntime = FakeRuntime
raise SystemExit(service.serve_prediction_service(
    data_dir=Path({str(tmp_path)!r}),
    prediction_config_path=Path({str(tmp_path / "prediction.json")!r}),
    port={port},
))
'''
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("shadow service did not bind")
        trigger.write_text("violate", encoding="utf-8")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                status, payload = _response(f"http://127.0.0.1:{port}/healthz")
                if status == 503:
                    assert payload["first_violation"] == violation
                    break
            except OSError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("failed Shadow health was not observable")
        status, payload = _response(f"http://127.0.0.1:{port}/api/prediction-arbitrage/state")
        assert status == 503
        assert payload == {"error": "shadow runtime is unavailable"}
        process.terminate()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert stopped.read_text(encoding="utf-8") == "stopped"


def test_signal_handler_is_installed_before_runtime_start(tmp_path: Path) -> None:
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"
    script = f'''\
from pathlib import Path
import os
import signal
import open_trader.prediction_service as service

started = Path({str(started)!r})
stopped = Path({str(stopped)!r})

class FakeRuntime:
    def __init__(self, **_kwargs):
        self.state = "NEW"
        self.shadow_evidence = {{"mode": "shadow", "first_violation": None, "codex": {{}}}}
    def start(self):
        started.write_text("started", encoding="utf-8")
        os.kill(os.getpid(), signal.SIGTERM)
        self.state = "RUNNING"
    def poll_shadow_failure(self):
        return None
    def stop(self):
        self.state = "STOPPED"
        stopped.write_text("stopped", encoding="utf-8")

service.PredictionRuntime = FakeRuntime
raise SystemExit(service.serve_prediction_service(
    data_dir=Path({str(tmp_path)!r}),
    prediction_config_path=Path({str(tmp_path / "prediction.json")!r}),
    port=0,
))
'''
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    assert process.wait(timeout=5) == 0
    assert started.read_text(encoding="utf-8") == "started"
    assert stopped.read_text(encoding="utf-8") == "stopped"


def test_sigterm_stops_shadow_runtime_and_releases_its_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "prediction_arbitrage" / "runtime.lock"
    marker = tmp_path / "stopped"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    script = f'''\
from pathlib import Path
from open_trader.prediction_runtime import _RuntimeOwnershipLock
import open_trader.prediction_service as service

data_dir = Path({str(tmp_path)!r})
marker = Path({str(marker)!r})

class FakeRuntime:
    def __init__(self, **_kwargs):
        self.state = "NEW"
        self.shadow_evidence = {{"mode": "shadow", "first_violation": None, "codex": {{}}}}
        self._lock = _RuntimeOwnershipLock(data_dir / "prediction_arbitrage" / "runtime.lock")
    def start(self):
        self._lock.acquire()
        self.state = "RUNNING"
    def poll_shadow_failure(self):
        return None
    def stop(self):
        self.state = "STOPPED"
        self._lock.release()
        marker.write_text("stopped", encoding="utf-8")

service.PredictionRuntime = FakeRuntime
raise SystemExit(service.serve_prediction_service(
    data_dir=data_dir,
    prediction_config_path=data_dir / "prediction.json",
    port={port},
))
'''
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("shadow service did not bind")
        process.terminate()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert marker.read_text(encoding="utf-8") == "stopped"
    lock = __import__("open_trader.prediction_runtime", fromlist=["_RuntimeOwnershipLock"])._RuntimeOwnershipLock(lock_path)
    lock.acquire()
    lock.release()
