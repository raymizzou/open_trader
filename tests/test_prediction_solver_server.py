from __future__ import annotations

import json
from pathlib import Path
import sys
from threading import Event, Lock

import pytest

from open_trader.prediction_n_leg import request_from_payload
from open_trader.prediction_solver import BenchmarkLimits
from open_trader.prediction_solver_server import SolverServerBusy, SolverServerOwner, SolverServerUnavailable
from open_trader.prediction_solver_worker import WorkerCleanupError, WorkerOutcome, WorkerRequest


def _request(request_id: str) -> WorkerRequest:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "prediction_n_leg_v1.json").read_text(
            encoding="utf-8"
        )
    )
    return WorkerRequest(
        request_id,
        "test",
        request_from_payload(fixture["cases"][0]["request"]),
        BenchmarkLimits(100, 200, 1_000_000, 4),
    )


class _Harness:
    instances: list["_Harness"] = []
    lock = Lock()
    running: list[str] = []
    started = Event()
    release = Event()
    block = False
    fail_close = False

    def __init__(self, _command: object) -> None:
        self.start_count = 0
        self.closed = False
        type(self).instances.append(self)

    def submit(self, request: WorkerRequest) -> WorkerOutcome:
        self.start_count = 1
        if type(self).block:
            with type(self).lock:
                type(self).running.append(request.request_id)
                if len(type(self).running) == 2:
                    type(self).started.set()
            assert type(self).release.wait(2)
        return WorkerOutcome(request.request_id, "OK", "COMPLETED", None, None, 0, False, True)

    def close(self) -> None:
        self.closed = True
        if type(self).fail_close:
            raise WorkerCleanupError("unproven")


@pytest.fixture(autouse=True)
def _reset_harness() -> None:
    _Harness.instances = []
    _Harness.running = []
    _Harness.started = Event()
    _Harness.release = Event()
    _Harness.block = False
    _Harness.fail_close = False


def test_two_slot_server_reuses_a_worker_and_closes_cleanly() -> None:
    server = SolverServerOwner(("solver",), harness_factory=_Harness)
    try:
        assert server.submit(_request("one")).result(timeout=2).status == "OK"
        assert server.submit(_request("two")).result(timeout=2).status == "OK"
        assert sum(server.worker_start_counts) == 1
    finally:
        server.close()

    assert server.closed is True


def test_two_slot_server_queues_one_third_task_then_bounds_pending_work() -> None:
    _Harness.block = True
    server = SolverServerOwner(("solver",), max_pending=1, harness_factory=_Harness)
    try:
        first = server.submit(_request("one"))
        second = server.submit(_request("two"))
        assert _Harness.started.wait(2)
        third = server.submit(_request("three"))
        with pytest.raises(SolverServerBusy):
            server.submit(_request("four"))
        _Harness.release.set()
        assert first.result(timeout=2).status == "OK"
        assert second.result(timeout=2).status == "OK"
        assert third.result(timeout=2).status == "OK"
    finally:
        server.close()


def test_cleanup_failure_closes_the_server_fail_closed() -> None:
    _Harness.fail_close = True
    server = SolverServerOwner(("solver",), harness_factory=_Harness)
    assert server.submit(_request("one")).result(timeout=2).status == "OK"
    with pytest.raises(SolverServerUnavailable):
        server.close()
    with pytest.raises(SolverServerUnavailable):
        server.submit(_request("two"))
