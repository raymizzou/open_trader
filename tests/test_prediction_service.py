from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from typing import Iterator, Mapping
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

import open_trader.prediction_service as prediction_service
from open_trader.prediction_read_model import (
    prediction_history_payload,
    prediction_state_payload,
)
from open_trader.prediction_service import create_prediction_server
from tests.test_prediction_arbitrage_execution import (
    _cross_service,
    execution_fixture,
    threshold_execution_fixture,
    wait_until_terminal,
)
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
def _running_server(runtime: object, **kwargs: object) -> Iterator[tuple[str, object]]:
    server = create_prediction_server(runtime=runtime, port=0, **kwargs)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _server(runtime: object, **kwargs: object) -> Iterator[str]:
    with _running_server(runtime, **kwargs) as (base, _server_instance):
        yield base


def _response(
    request: str | Request, *, timeout: float = 15
) -> tuple[int, dict[str, object]]:
    status, payload, _headers = _response_with_headers(request, timeout=timeout)
    return status, payload


def _response_with_headers(
    request: str | Request, *, timeout: float = 15
) -> tuple[int, dict[str, object], Mapping[str, str]]:
    try:
        with urlopen(request, timeout=timeout) as response:
            return (
                response.status,
                json.loads(response.read().decode("utf-8")),
                dict(response.headers.items()),
            )
    except HTTPError as error:
        return (
            error.code,
            json.loads(error.read().decode("utf-8")),
            dict(error.headers.items()),
        )


def _socket_fd_count() -> int:
    count = 0
    for name in os.listdir("/dev/fd"):
        try:
            count += stat.S_ISSOCK(os.fstat(int(name)).st_mode)
        except (OSError, ValueError):
            continue
    return count


def test_socket_fd_count_does_not_leak_non_socket_descriptors() -> None:
    read_fd, write_fd = os.pipe()
    try:
        before = len(os.listdir("/dev/fd"))
        for _ in range(3):
            _socket_fd_count()
        assert len(os.listdir("/dev/fd")) == before
    finally:
        os.close(read_fd)
        os.close(write_fd)


def _handler_thread_ids() -> set[int | None]:
    return {
        thread.ident
        for thread in threading.enumerate()
        if "process_request_thread" in thread.name
    }


class _ProductionExecution:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, Mapping[str, object]]] = []
        self.mode_result: dict[str, object] = {"state": "ok", "mode": "manual"}
        self.error: Exception | None = None

    def preview(self, opportunity_id: str) -> dict[str, object]:
        self.calls.append(("preview", opportunity_id, {}))
        return {
            "state": "previewed",
            "preview_id": "preview-1",
            "opportunity_id": opportunity_id,
        }

    def confirm(self, preview_id: str, idempotency_key: str) -> dict[str, object]:
        self.calls.append(
            (
                "confirm",
                {"preview_id": preview_id, "idempotency_key": idempotency_key},
                {},
            )
        )
        return {
            "state": "validating",
            "execution_id": "execution-1",
            "preview_id": preview_id,
            "idempotency_key": idempotency_key,
        }

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


def test_global_http_capacity_rejects_overflow_and_releases_read_contexts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class TrackingReadStore:
        def __init__(self) -> None:
            self.path = tmp_path / "tracked_reads.sqlite3"
            with sqlite3.connect(self.path) as connection:
                connection.execute("CREATE TABLE reads (value INTEGER)")
            self.lock = threading.Lock()
            self.live_contexts = 0

        @contextmanager
        def read_context(self) -> Iterator[None]:
            connection = sqlite3.connect(self.path)
            with self.lock:
                self.live_contexts += 1
            try:
                yield
            finally:
                connection.close()
                with self.lock:
                    self.live_contexts -= 1

    tracking_store = TrackingReadStore()
    counts = {"active": 0, "max_active": 0, "attempts": 0}
    counts_lock = threading.Lock()
    entered = threading.Event()
    overflow_attempted = threading.Event()
    release = threading.Event()

    def blocked_state_payload(**_kwargs: object) -> dict[str, object]:
        with tracking_store.read_context():
            with counts_lock:
                counts["active"] += 1
                counts["max_active"] = max(counts["max_active"], counts["active"])
                if counts["active"] == 8:
                    entered.set()
            try:
                assert release.wait(timeout=60)
                return {"state": "blocked"}
            finally:
                with counts_lock:
                    counts["active"] -= 1

    def overflow_request(base: str) -> tuple[int, dict[str, object], Mapping[str, str]]:
        with counts_lock:
            counts["attempts"] += 1
            if counts["attempts"] == 40:
                overflow_attempted.set()
        return _response_with_headers(base + "/api/prediction-arbitrage/state", timeout=60)

    monkeypatch.setattr(prediction_service, "prediction_state_payload", blocked_state_payload)
    with _running_server(_Runtime()) as (base, server):
        baseline_handler_ids = _handler_thread_ids()
        baseline_socket_fds = _socket_fd_count()
        try:
            with ThreadPoolExecutor(max_workers=48) as clients:
                leader_timeout = 60
                leaders = [
                    clients.submit(
                        _response,
                        base + "/api/prediction-arbitrage/state",
                        timeout=leader_timeout,
                    )
                    for _ in range(8)
                ]
                assert entered.wait(timeout=30)
                overflow = [clients.submit(overflow_request, base) for _ in range(40)]
                assert overflow_attempted.wait(timeout=30)

                assert counts["max_active"] == 8
                assert server.http_load_snapshot()["active"] == 8  # type: ignore[attr-defined]
                assert len(_handler_thread_ids() - baseline_handler_ids) == 8
                overflow_results = [future.result(timeout=60) for future in overflow]
                overflow_statuses = [result[0] for result in overflow_results]
                overflow_payloads = [result[1] for result in overflow_results]
                overflow_retry_after = [result[2]["Retry-After"] for result in overflow_results]
                overflow_connections = [result[2]["Connection"] for result in overflow_results]
                assert overflow_statuses == [503] * 40
                assert overflow_payloads == [{"error": "prediction service busy"}] * 40
                assert overflow_retry_after == ["1"] * 40
                assert overflow_connections == ["close"] * 40
                assert server.http_load_snapshot()["overload_rejections"] == 40  # type: ignore[attr-defined]

                release.set()
                leader_results = [
                    future.result(timeout=leader_timeout) for future in leaders
                ]
                assert [result[0] for result in leader_results] == [200] * 8
                deadline = time.monotonic() + 5
                while server.http_load_snapshot()["active"] != 0 and time.monotonic() < deadline:  # type: ignore[attr-defined]
                    time.sleep(0.01)
                assert server.http_load_snapshot()["active"] == 0  # type: ignore[attr-defined]
                while (
                    _handler_thread_ids() - baseline_handler_ids
                    or _socket_fd_count() > baseline_socket_fds
                ) and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert _handler_thread_ids() == baseline_handler_ids
                assert _socket_fd_count() == baseline_socket_fds
                assert tracking_store.live_contexts == 0
        finally:
            release.set()


def test_mixed_http_capacity_shares_slots_and_exposes_health_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _ProductionRuntime()
    counts = {"state": 0, "auth": 0, "body": 0, "preview": 0}
    counts_lock = threading.Lock()
    state_entered = threading.Event()
    preview_entered = threading.Event()
    preview_reentered = threading.Event()
    release_slots = threading.Semaphore(0)

    def blocked_state_payload(**_kwargs: object) -> dict[str, object]:
        with counts_lock:
            counts["state"] += 1
            if counts["state"] == 4:
                state_entered.set()
        assert release_slots.acquire(timeout=10)
        return {"state": "blocked"}

    def blocked_preview(opportunity_id: str) -> dict[str, object]:
        with counts_lock:
            counts["preview"] += 1
            if counts["preview"] == 4:
                preview_entered.set()
            if counts["preview"] == 5:
                preview_reentered.set()
        assert release_slots.acquire(timeout=10)
        return {
            "state": "previewed",
            "preview_id": "preview-1",
            "opportunity_id": opportunity_id,
        }

    monkeypatch.setattr(prediction_service, "prediction_state_payload", blocked_state_payload)
    monkeypatch.setattr(runtime.execution, "preview", blocked_preview)
    with _running_server(
        runtime,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    ) as (base, server):
        handler_class = server.RequestHandlerClass  # type: ignore[attr-defined]
        original_auth = handler_class._require_production_auth
        original_read = handler_class._read_json_body

        def traced_auth(handler: object) -> None:
            with counts_lock:
                counts["auth"] += 1
            original_auth(handler)

        def traced_read(handler: object) -> dict[str, object]:
            with counts_lock:
                counts["body"] += 1
            return original_read(handler)

        monkeypatch.setattr(handler_class, "_require_production_auth", traced_auth)
        monkeypatch.setattr(handler_class, "_read_json_body", traced_read)
        try:
            with ThreadPoolExecutor(max_workers=10) as clients:
                state_calls = [
                    clients.submit(_response, base + "/api/prediction-arbitrage/state")
                    for _ in range(4)
                ]
                preview_calls = [
                    clients.submit(
                        _response,
                        _production_request(
                            base,
                            "/api/prediction-arbitrage/preview",
                            data=b'{"opportunity_id":"opp-1"}',
                        ),
                    )
                    for _ in range(4)
                ]
                assert state_entered.wait(timeout=15)
                assert preview_entered.wait(timeout=15)

                overflow_get = clients.submit(
                    _response_with_headers,
                    base + "/api/prediction-arbitrage/state",
                    timeout=15,
                )
                overflow_post = clients.submit(
                    _response_with_headers,
                    _production_request(
                        base,
                        "/api/prediction-arbitrage/preview",
                        data=b'{"opportunity_id":"opp-1"}',
                    ),
                    timeout=15,
                )
                assert server.http_load_snapshot()["active"] == 8  # type: ignore[attr-defined]

                for status, payload, headers in (
                    overflow_get.result(timeout=15),
                    overflow_post.result(timeout=15),
                ):
                    assert status == 503
                    assert payload == {"error": "prediction service busy"}
                    assert headers["Retry-After"] == "1"
                    assert headers["Connection"] == "close"
                assert counts == {"state": 4, "auth": 4, "body": 4, "preview": 4}
                assert server.http_load_snapshot()["overload_rejections"] == 2  # type: ignore[attr-defined]

                release_slots.release()
                replacement = clients.submit(
                    _response,
                    _production_request(
                        base,
                        "/api/prediction-arbitrage/preview",
                        data=b'{"opportunity_id":"opp-1"}',
                    ),
                )
                assert preview_reentered.wait(timeout=15)

                for _ in range(8):
                    release_slots.release()
                assert [future.result(timeout=15)[0] for future in state_calls] == [200] * 4
                assert [future.result(timeout=15)[0] for future in preview_calls] == [200] * 4
                assert replacement.result(timeout=15)[0] == 200
                deadline = time.monotonic() + 5
                while server.http_load_snapshot()["active"] != 0 and time.monotonic() < deadline:  # type: ignore[attr-defined]
                    time.sleep(0.01)
                assert server.http_load_snapshot()["active"] == 0  # type: ignore[attr-defined]

                health_status, health = _response(base + "/healthz")
                assert health_status == 200
                assert health["http_load"] == {
                    "limit": 8,
                    "active": 1,
                    "overload_rejections": 2,
                    "history_cache_hits": 0,
                    "history_cache_misses": 0,
                }
        finally:
            for _ in range(16):
                release_slots.release()


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
    assert payload["source_state"] in {"clean", "dirty"}
    assert "release_schema_version" not in payload
    assert "reader_generation" not in payload
    assert "contract_generation" not in payload


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


def test_history_single_flight_reuses_identical_inflight_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    calls_lock = threading.Lock()
    leader_entered = threading.Event()
    release_leader = threading.Event()

    def controlled_history(_store: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        leader_entered.set()
        assert release_leader.wait(timeout=5)
        return {"call": call}

    monkeypatch.setattr(prediction_service, "prediction_history_payload", controlled_history)
    with _running_server(_Runtime()) as (base, server):
        try:
            with ThreadPoolExecutor(max_workers=8) as clients:
                requests = [
                    clients.submit(
                        _response,
                        base + "/api/prediction-arbitrage/history?kind=signals&limit=1&offset=0",
                    )
                    for _ in range(8)
                ]
                assert leader_entered.wait(timeout=5)
                for _ in range(100):
                    if server.http_load_snapshot()["history_cache_hits"] == 7:  # type: ignore[attr-defined]
                        break
                    time.sleep(0.01)
                assert calls == 1
                load = server.http_load_snapshot()  # type: ignore[attr-defined]
                assert load["history_cache_misses"] == 1
                assert load["history_cache_hits"] == 7

                release_leader.set()
                assert [future.result(timeout=5) for future in requests] == [
                    (200, {"call": 1})
                ] * 8
        finally:
            release_leader.set()


def test_history_cache_ttl_expires_and_keeps_pagination_keys_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    clock = [0.0]

    def controlled_history(_store: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"call": calls}

    monkeypatch.setattr(prediction_service, "prediction_history_payload", controlled_history)
    monkeypatch.setattr(prediction_service.time, "monotonic", lambda: clock[0])
    with _running_server(_Runtime()) as (base, server):
        def get_history(query: str) -> tuple[int, dict[str, object]]:
            return _response(base + "/api/prediction-arbitrage/history?" + query)

        first_payload = (200, {"call": 1})
        assert get_history("kind=signals&limit=1&offset=0") == first_payload
        assert calls == 1

        clock[0] = 0.999
        assert get_history("kind=signals&limit=1&offset=0") == first_payload
        assert calls == 1

        clock[0] = 1.001
        second_payload = (200, {"call": 2})
        assert get_history("kind=signals&limit=1&offset=0") == second_payload
        assert calls == 2

        assert get_history("kind=executions&limit=1&offset=0") == (200, {"call": 3})
        assert get_history("kind=executions&limit=1&offset=1") == (200, {"call": 4})
        assert calls == 4
        assert server.http_load_snapshot()["history_cache_misses"] == 4  # type: ignore[attr-defined]


def test_history_flight_failure_wakes_followers_and_recomputes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    calls_lock = threading.Lock()
    leader_entered = threading.Event()
    release_leader = threading.Event()

    def failing_history(_store: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        if call == 1:
            leader_entered.set()
            assert release_leader.wait(timeout=5)
            raise sqlite3.OperationalError("database is locked")
        return {"call": call}

    monkeypatch.setattr(prediction_service, "prediction_history_payload", failing_history)
    query = "kind=signals&limit=1&offset=0"
    key = ("signals", 1, 0)
    with _running_server(_Runtime()) as (base, server):
        try:
            with ThreadPoolExecutor(max_workers=8) as clients:
                requests = [
                    clients.submit(
                        _response,
                        base + "/api/prediction-arbitrage/history?" + query,
                    )
                    for _ in range(8)
                ]
                assert leader_entered.wait(timeout=5)
                for _ in range(100):
                    if server.http_load_snapshot()["history_cache_hits"] == 7:  # type: ignore[attr-defined]
                        break
                    time.sleep(0.01)
                release_leader.set()

                assert [future.result(timeout=5) for future in requests] == [
                    (503, {"error": "prediction history unavailable"})
                ] * 8
            assert calls == 1
            assert key not in server._history_cache  # type: ignore[attr-defined]
            assert _response(base + "/api/prediction-arbitrage/history?" + query) == (
                200,
                {"call": 2},
            )
            assert calls == 2
        finally:
            release_leader.set()


def test_history_wait_timeout_keeps_the_leader_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    leader_entered = threading.Event()
    release_leader = threading.Event()

    def blocked_history(_store: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        leader_entered.set()
        assert release_leader.wait(timeout=5)
        return {"call": calls}

    monkeypatch.setattr(prediction_service, "prediction_history_payload", blocked_history)
    monkeypatch.setattr(prediction_service, "_HISTORY_WAIT_SECONDS", 0.05)
    query = "kind=signals&limit=1&offset=0"
    key = ("signals", 1, 0)
    with _running_server(_Runtime()) as (base, server):
        try:
            with ThreadPoolExecutor(max_workers=2) as clients:
                leader = clients.submit(
                    _response,
                    base + "/api/prediction-arbitrage/history?" + query,
                )
                assert leader_entered.wait(timeout=5)
                follower = clients.submit(
                    _response,
                    base + "/api/prediction-arbitrage/history?" + query,
                )
                assert follower.result(timeout=5) == (
                    503,
                    {"error": "prediction history unavailable"},
                )
                assert calls == 1
                assert key in server._history_flights  # type: ignore[attr-defined]
                assert server.http_load_snapshot()["active"] == 1  # type: ignore[attr-defined]

                release_leader.set()
                assert leader.result(timeout=5) == (200, {"call": 1})
        finally:
            release_leader.set()


def test_history_cache_never_serves_expired_payload_after_recompute_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    clock = [0.0]

    def stale_history(_store: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("database is locked")
        return {"call": calls}

    monkeypatch.setattr(prediction_service, "prediction_history_payload", stale_history)
    monkeypatch.setattr(prediction_service.time, "monotonic", lambda: clock[0])
    with _running_server(_Runtime()) as (base, _server_instance):
        path = base + "/api/prediction-arbitrage/history?kind=signals&limit=1&offset=0"
        assert _response(path) == (200, {"call": 1})
        clock[0] = 1.001
        assert _response(path) == (
            503,
            {"error": "prediction history unavailable"},
        )
        assert calls == 2


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("kind=signals&limit=0", "limit must be positive"),
        ("kind=unknown&limit=1", "kind must be signals, executions, or incidents"),
        ("kind=unknown&limit=0", "limit must be positive"),
    ),
)
def test_shadow_history_rejects_invalid_query(query: str, expected: str) -> None:
    with _running_server(_Runtime()) as (base, server):
        status, payload = _response(
            base + "/api/prediction-arbitrage/history?" + query
        )

    assert status == 400
    assert payload == {"error": expected}
    assert server.http_load_snapshot()["history_cache_hits"] == 0  # type: ignore[attr-defined]
    assert server.http_load_snapshot()["history_cache_misses"] == 0  # type: ignore[attr-defined]


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
def test_production_mutation_rejects_invalid_request_identity_before_dispatch(
    header: str, value: str
) -> None:
    with _production_server() as (base, runtime):
        status, _payload = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/preview",
                headers={header: value},
            )
        )

    assert status == 403
    assert runtime.execution.calls == []


def test_production_auth_precedes_body_limits_and_route_dispatch() -> None:
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


@pytest.mark.parametrize(
    ("path", "body"),
    (
        (
            "/api/prediction-arbitrage/preview",
            b'{"opportunity_id":"opp-1","unexpected":true}',
        ),
        (
            "/api/prediction-arbitrage/executions",
            b'{"preview_id":"preview-1"}',
        ),
    ),
)
def test_production_execution_mutations_reject_invalid_schema(
    path: str, body: bytes
) -> None:
    with _production_server() as (base, runtime):
        status, _payload = _response(
            _production_request(base, path, data=body)
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


def test_production_exposes_prediction_preview_and_confirmation() -> None:
    with _production_server() as (base, runtime):
        preview_status, preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/preview",
                data=b'{"opportunity_id":"opp-1"}',
            )
        )
        execution_status, execution = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/executions",
                data=b'{"preview_id":"preview-1","idempotency_key":"key-1"}',
            )
        )

    assert preview_status == execution_status == 200
    assert preview["preview_id"] == execution["preview_id"] == "preview-1"
    assert execution["idempotency_key"] == "key-1"
    assert runtime.execution.calls == [
        ("preview", "opp-1", {}),
        (
            "confirm",
            {"preview_id": "preview-1", "idempotency_key": "key-1"},
            {},
        ),
    ]


def test_production_http_confirmation_preserves_execution_idempotency(
    tmp_path: Path,
) -> None:
    execution_service, trading, store, monitor = execution_fixture(
        tmp_path, result="both_rejected"
    )
    runtime = _ProductionRuntime()
    runtime.store = store  # type: ignore[assignment]
    runtime.monitor = monitor  # type: ignore[assignment]
    runtime.execution = execution_service  # type: ignore[assignment]
    runtime.cross_venue_monitor = None  # type: ignore[assignment]

    with _production_server(runtime) as (base, _runtime):
        rejection_status, rejection = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/preview",
                data=b'{"opportunity_id":"missing"}',
            )
        )
        preview_status, preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/preview",
                data=b'{"opportunity_id":"opp-1"}',
            )
        )
        request = json.dumps(
            {
                "preview_id": preview["preview_id"],
                "idempotency_key": "same-request",
            }
        ).encode("utf-8")
        first_status, first = _response(
            _production_request(
                base, "/api/prediction-arbitrage/executions", data=request
            )
        )
        second_status, second = _response(
            _production_request(
                base, "/api/prediction-arbitrage/executions", data=request
            )
        )

    final = wait_until_terminal(execution_service, str(first["execution_id"]))
    assert rejection_status == preview_status == first_status == second_status == 200
    assert rejection == {"state": "rejected", "reason": "opportunity_unavailable"}
    assert preview["total_max_cost"] == "8.00"
    assert preview["minimum_profit"] == "2.00"
    assert preview["wallet_address"] == "0x1111…1111"
    assert "intent" not in preview
    assert second["execution_id"] == first["execution_id"]
    assert final["state"] == "both_rejected"
    assert trading.batch_calls == 1


def test_production_http_previews_llm_relationship_economics(tmp_path: Path) -> None:
    execution_service, _trading, store, _monitor = threshold_execution_fixture(
        tmp_path
    )
    runtime = _ProductionRuntime()
    runtime.store = store  # type: ignore[assignment]
    runtime.execution = execution_service  # type: ignore[assignment]

    with _production_server(runtime) as (base, _runtime):
        status, preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/preview",
                data=b'{"opportunity_id":"threshold-opp-1"}',
            )
        )

    assert status == 200
    assert preview["state"] == "previewed"
    assert preview["intent_type"] == "threshold_hedge"
    assert preview["total_max_cost"] == "2.12"
    assert preview["minimum_profit"] == "7.88"
    assert preview["llm_status"] == "approved"


def test_production_http_previews_cross_venue_economics(tmp_path: Path) -> None:
    execution_service, store, _trading, _cross, _predict = _cross_service(tmp_path)
    runtime = _ProductionRuntime()
    runtime.store = store  # type: ignore[assignment]
    runtime.execution = execution_service  # type: ignore[assignment]

    with _production_server(runtime) as (base, _runtime):
        status, preview = _response(
            _production_request(
                base,
                "/api/prediction-arbitrage/preview",
                data=(
                    b'{"opportunity_id":'
                    b'"cross:public-pair:PREDICT_YES_POLYMARKET_NO"}'
                ),
            )
        )

    assert status == 200
    assert preview["state"] == "previewed"
    assert preview["market_type"] == "cross_venue_yes_no"
    assert preview["maximum_total_cost"] == "4.80"
    assert preview["minimum_profit"] == "0.20"
    assert [leg["exchange"] for leg in preview["buy_legs"]] == [
        "predict.fun",
        "polymarket",
    ]


def test_production_rejects_unknown_prediction_mutation() -> None:
    with _production_server() as (base, runtime):
        status, payload = _response(
            _production_request(base, "/api/prediction-arbitrage/unknown")
        )

    assert status == 404
    assert payload == {"error": "not found"}
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
    release_manifest = (
        Path(__file__).resolve().parents[1] / "ops" / "prediction-service-release.json"
    )
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
    release_manifest_path=Path({str(release_manifest)!r}),
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
            release_manifest_path=Path(__file__).resolve().parents[1]
            / "ops"
            / "prediction-service-release.json",
        )

    assert len(instances) == 1
    assert instances[0].state == "STOPPED"


def test_production_service_requires_release_manifest_before_runtime_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_service as service

    monkeypatch.setattr(
        service,
        "PredictionRuntime",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("runtime constructed")),
    )
    with pytest.raises(ValueError, match="release manifest is required"):
        service.serve_prediction_service(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            port=0,
            mode="production",
        )


def test_cli_passes_production_config_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    import open_trader.prediction_service as service

    captured: dict[str, object] = {}

    def serve(**kwargs: object) -> int:
        captured.update(kwargs)
        return 7

    monkeypatch.setattr(service, "serve_prediction_service", serve)

    result = service.main(
        [
            "--mode",
            "production",
            "--data-dir",
            "/tmp/data",
            "--config",
            "/tmp/prediction.json",
            "--release-manifest",
            "/tmp/release.json",
            "--notifier-config",
            "/tmp/daily_premarket.env",
        ]
    )

    assert result == 7
    assert captured["release_manifest_path"] == Path("/tmp/release.json")
    assert captured["notifier_config_path"] == Path("/tmp/daily_premarket.env")


def test_production_service_injects_notifier_from_notifier_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_trader.prediction_service as service

    instances = []
    notifier = object()
    notifier_config = tmp_path / "daily_premarket.env"
    loaded_config = object()
    loaded_paths: list[tuple[Path, bool]] = []

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

    monkeypatch.setattr(service, "PredictionRuntime", FakeRuntime)
    def load_config(path: Path, *, dry_run: bool) -> object:
        loaded_paths.append((path, dry_run))
        return loaded_config

    def build_configured_notifier(config: object) -> object:
        assert config is loaded_config
        return notifier

    monkeypatch.setattr(service, "load_env_config", load_config, raising=False)
    monkeypatch.setattr(
        service, "build_notifier", build_configured_notifier, raising=False
    )
    monkeypatch.setattr(
        service, "create_prediction_server",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("bind failed")),
    )

    with pytest.raises(OSError, match="bind failed"):
        service.serve_prediction_service(
            data_dir=tmp_path,
            prediction_config_path=tmp_path / "prediction.json",
            notifier_config_path=notifier_config,
            port=0,
            mode="production",
            release_manifest_path=Path(__file__).resolve().parents[1]
            / "ops"
            / "prediction-service-release.json",
        )

    assert instances[0].kwargs["notifier"] is notifier
    assert loaded_paths == [(notifier_config, False)]


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
        assert kwargs["runtime_metadata"] == {
            **metadata,
            "release_schema_version": "open_trader.prediction_service.release.v1",
            "reader_generation": 1,
            "contract_generation": 1,
        }
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
            release_manifest_path=Path(__file__).resolve().parents[1]
            / "ops"
            / "prediction-service-release.json",
        )

    assert instances[0].kwargs["git_sha"] == "abc123"
    assert instances[0].kwargs["reader_generation"] == 1
    assert instances[0].state == "STOPPED"
    assert {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    } == previous
