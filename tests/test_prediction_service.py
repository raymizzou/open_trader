from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Iterator, Mapping
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
def _server(runtime: object, **kwargs: object) -> Iterator[str]:
    server = create_prediction_server(runtime=runtime, port=0, **kwargs)  # type: ignore[arg-type]
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


class _ProductionExecution:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, Mapping[str, object]]] = []
        self.mode_result: dict[str, object] = {"state": "ok", "mode": "manual"}
        self.error: Exception | None = None

    def set_validation_mode(
        self, mode: str, *, audit: Mapping[str, object]
    ) -> dict[str, object]:
        self.calls.append(("mode", mode, audit))
        if self.error is not None:
            raise self.error
        return self.mode_result

    def reset_breaker(
        self, incident_id: str, *, audit: Mapping[str, object]
    ) -> dict[str, object]:
        self.calls.append(("reset", incident_id, audit))
        return {"state": "ready", "incident_id": incident_id}

    def cleanup_predict_allowance(
        self, *, confirm: bool, audit: Mapping[str, object]
    ) -> dict[str, object]:
        self.calls.append(("cleanup", confirm, audit))
        return {"state": "ready"}

    def pause_cross_auto(
        self, *, audit: Mapping[str, object]
    ) -> dict[str, object]:
        self.calls.append(("pause", True, audit))
        return {"state": "ready"}

    def cross_auto_status(self) -> dict[str, object]:
        return {}


class _ProductionStore(_Store):
    def safety_policy(self) -> dict[str, object]:
        return {"fingerprint": "policy-1"}


class _ProductionRuntime:
    mode = "production"

    def __init__(self, *, state: str = "RUNNING", owner: bool = True) -> None:
        self.state = state
        self.production_owner = owner
        self.store = _ProductionStore()
        self.monitor = _Monitor()
        self.execution = _ProductionExecution()
        self.cross_venue_monitor = _CrossVenueMonitor()


def _production_request(
    base: str,
    path: str,
    data: bytes = b'{"mode":"manual"}',
    *,
    headers: Mapping[str, str] | None = None,
) -> Request:
    request_headers = {
        "Content-Type": "application/json",
        "Cookie": "ot_prediction_session=session-token",
        "Origin": base,
        "X-CSRF-Token": "csrf-token",
    }
    request_headers.update(headers or {})
    return Request(
        base + path,
        data=data,
        headers=request_headers,
        method="POST",
    )


@contextmanager
def _production_server(
    runtime: _ProductionRuntime | None = None,
) -> Iterator[tuple[str, _ProductionRuntime]]:
    current = runtime or _ProductionRuntime()
    with _server(
        current,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as base:
        yield base, current


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


@pytest.mark.parametrize(
    ("header", "value"),
    (
        ("Host", "evil.example"),
        ("Origin", "https://evil.example"),
        ("Cookie", "ot_prediction_session=wrong"),
        ("X-CSRF-Token", "wrong"),
    ),
)
def test_production_control_rejects_invalid_request_identity_before_dispatch(
    header: str, value: str
) -> None:
    with _production_server() as (base, runtime):
        status, _payload = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/mode",
                headers={header: value},
            )
        )

    assert status == 403
    assert runtime.execution.calls == []


def test_production_auth_precedes_body_limits_and_disabled_route_dispatch() -> None:
    with _production_server() as (base, runtime):
        parsed = urlsplit(base)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as connection:
            connection.sendall(
                (
                    "POST /api/prediction-arbitrage/preview HTTP/1.1\r\n"
                    f"Host: {parsed.netloc}\r\n"
                    "Origin: https://evil.example\r\n"
                    "Content-Length: 9999999\r\n\r\n"
                ).encode("ascii")
            )
            response = connection.recv(1024)

    assert b"403" in response.split(b"\r\n", 1)[0]
    assert runtime.execution.calls == []


@pytest.mark.parametrize(
    "body",
    (
        b"{}",
        b'{"mode":"manual","extra":true}',
        b'[{"mode":"manual"}]',
        b"not-json",
    ),
)
def test_production_control_rejects_invalid_json_schema(body: bytes) -> None:
    with _production_server() as (base, runtime):
        status, _payload = _response(
            _production_request(
                base, "/api/prediction-arbitrage/mode", data=body
            )
        )

    assert status == 400
    assert runtime.execution.calls == []


def test_production_control_rejects_body_over_one_mib_before_reading() -> None:
    with _production_server() as (base, runtime):
        parsed = urlsplit(base)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as connection:
            connection.sendall(
                (
                    "POST /api/prediction-arbitrage/mode HTTP/1.1\r\n"
                    f"Host: {parsed.netloc}\r\n"
                    f"Origin: {base}\r\n"
                    "Cookie: ot_prediction_session=session-token\r\n"
                    "X-CSRF-Token: csrf-token\r\n"
                    "Content-Length: 1048577\r\n\r\n"
                ).encode("ascii")
            )
            response = connection.recv(1024)

    assert b"413" in response.split(b"\r\n", 1)[0]
    assert runtime.execution.calls == []


@pytest.mark.parametrize(
    ("path", "expected"),
    (
        ("/api/prediction-arbitrage/unknown", 404),
        ("/api/prediction-arbitrage/preview", 503),
        ("/api/prediction-arbitrage/executions", 503),
    ),
)
def test_production_exposes_only_four_control_mutations(
    path: str, expected: int
) -> None:
    with _production_server() as (base, runtime):
        status, payload = _response(_production_request(base, path))

    assert status == expected
    if expected == 503:
        assert payload == {"error": "prediction mutation is unavailable"}
    assert runtime.execution.calls == []


def test_production_control_maps_conflict_and_storage_failure() -> None:
    runtime = _ProductionRuntime()
    with _production_server(runtime) as (base, _runtime):
        runtime.execution.mode_result = {
            "state": "busy",
            "reason": "control_in_progress",
        }
        busy_status, _busy = _response(
            _production_request(base, "/api/prediction-arbitrage/mode")
        )
        runtime.execution.error = sqlite3.OperationalError("database is locked")
        unavailable_status, _unavailable = _response(
            _production_request(base, "/api/prediction-arbitrage/mode")
        )

    assert busy_status == 409
    assert unavailable_status == 503
    assert runtime.execution.calls[0][2] == {
        "actor": "local_operator",
        "git_sha": "abc123",
        "safety_fingerprint": "policy-1",
    }


def test_production_reads_and_controls_fail_closed_after_owner_loss() -> None:
    runtime = _ProductionRuntime()
    with _production_server(runtime) as (base, _runtime):
        runtime.production_owner = False
        health_status, health = _response(base + "/healthz")
        state_status, state = _response(
            base + "/api/prediction-arbitrage/state"
        )
        mutation_status, mutation = _response(
            _production_request(base, "/api/prediction-arbitrage/mode")
        )

    assert health_status == state_status == mutation_status == 503
    assert health["production_owner"] is False
    assert state == mutation == {"error": "production runtime is unavailable"}
    assert runtime.execution.calls == []


@pytest.mark.parametrize(
    ("state", "owner"),
    (("NOT_READY", True), ("FAILED", True), ("RUNNING", False)),
)
def test_production_server_refuses_to_bind_without_running_owner(
    state: str, owner: bool
) -> None:
    with pytest.raises(RuntimeError, match="not ready"):
        create_prediction_server(
            runtime=_ProductionRuntime(state=state, owner=owner),  # type: ignore[arg-type]
            port=0,
        )


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


@pytest.mark.parametrize("mode", ("shadow", "production"))
def test_sigterm_stops_prediction_runtime_and_releases_its_lock(
    tmp_path: Path, mode: str
) -> None:
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
    def __init__(self, **kwargs):
        self.state = "NEW"
        self.mode = kwargs["mode"]
        self.production_owner = False
        self.shadow_evidence = {{"mode": self.mode, "first_violation": None, "codex": {{}}}}
        self._lock = _RuntimeOwnershipLock(data_dir / "prediction_arbitrage" / "runtime.lock")
    def start(self):
        self._lock.acquire()
        self.state = "RUNNING"
        self.production_owner = self.mode == "production"
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
    mode={mode!r},
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


@pytest.mark.parametrize(
    ("state", "owner"),
    (("NOT_READY", True), ("FAILED", True), ("RUNNING", False)),
)
def test_production_owner_must_be_ready_before_server_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    owner: bool,
) -> None:
    import open_trader.prediction_service as service

    instances = []

    class FakeRuntime:
        def __init__(self, **_kwargs: object) -> None:
            self.state = "NEW"
            self.production_owner = False
            instances.append(self)

        def start(self) -> None:
            self.state = state
            self.production_owner = owner

        def stop(self) -> None:
            self.state = "STOPPED"

    def unexpected_bind(**_kwargs: object) -> object:
        raise AssertionError("server must not bind")

    monkeypatch.setattr(service, "PredictionRuntime", FakeRuntime)
    monkeypatch.setattr(service, "create_prediction_server", unexpected_bind)

    with pytest.raises(RuntimeError, match="not ready"):
        service.serve_prediction_service(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            port=0,
            mode="production",
        )

    assert len(instances) == 1
    assert instances[0].state == "STOPPED"


def test_production_bind_failure_stops_runtime_and_uses_one_metadata_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_service as service

    metadata = {
        "pid": 123,
        "cwd": "/tmp/accepted",
        "git_sha": "abc123",
        "started_at": "2026-08-11T00:00:00+08:00",
    }
    instances = []

    class FakeRuntime:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.state = "NEW"
            self.production_owner = False
            instances.append(self)

        def start(self) -> None:
            self.state = "RUNNING"
            self.production_owner = True

        def stop(self) -> None:
            self.state = "STOPPED"

    def fail_bind(**kwargs: object) -> object:
        assert kwargs["runtime"] is instances[0]
        assert kwargs["runtime_metadata"] == metadata
        raise OSError("bind failed")

    monkeypatch.setattr(service, "PredictionRuntime", FakeRuntime)
    monkeypatch.setattr(service, "create_prediction_server", fail_bind)
    monkeypatch.setattr(service, "_runtime_metadata", lambda: metadata)
    previous = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }

    with pytest.raises(OSError, match="bind failed"):
        service.serve_prediction_service(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            port=0,
            mode="production",
        )

    assert instances[0].kwargs["git_sha"] == "abc123"
    assert instances[0].state == "STOPPED"
    assert {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    } == previous
