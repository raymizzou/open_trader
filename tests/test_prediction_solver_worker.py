from __future__ import annotations

import json
import os
from pathlib import Path
import io
import resource
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from open_trader.prediction_solver import BENCHMARK_PROTOCOL_V1
from open_trader.prediction_solver_worker import (
    MAX_LINE_BYTES,
    WorkerHarness,
    WorkerProtocolError,
    _PipeReader,
    decode_handshake_line,
    decode_request_line,
    decode_response_line,
    encode_request_line,
    process_group_rss_kib,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "prediction_n_leg_v1.json"


def _canonical_request() -> dict[str, object]:
    payload = json.loads(FIXTURE.read_text())
    return payload["cases"][0]["request"]


def _limits(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "soft_time_limit_ms": 100,
        "hard_time_limit_ms": 500,
        "memory_limit_bytes": 1024 * 1024 * 1024 * 1024,
        "max_constraint_generation_rounds": 8,
    }
    value.update(changes)
    return value


def _request(request_id: str = "request-1", *, backend: str = "test", **changes: object) -> dict[str, object]:
    return {
        "backend": backend,
        "limits": _limits(**changes),
        "protocol": BENCHMARK_PROTOCOL_V1,
        "request": _canonical_request(),
        "request_id": request_id,
    }


def _test_command(mode: str) -> list[str]:
    return [sys.executable, "-m", "open_trader.prediction_solver_worker", "--test-mode", mode]


def test_protocol_request_round_trips_a_canonical_request() -> None:
    encoded = encode_request_line(_request())
    decoded = decode_request_line(encoded)

    assert decoded.request_id == "request-1"
    assert decoded.backend == "test"
    assert decoded.request.schema_version == "open_trader.prediction_n_leg.request.v1"
    assert decoded.limits.soft_time_limit_ms == 100


@pytest.mark.parametrize(
    "mutator",
    (
        lambda value: value.pop("request_id"),
        lambda value: value.__setitem__("unexpected", 1),
        lambda value: value["limits"].__setitem__("soft_time_limit_ms", True),
        lambda value: value["limits"].__setitem__("soft_time_limit_ms", 0),
        lambda value: value.__setitem__("protocol", "unsupported.protocol.v9"),
        lambda value: value.__setitem__("backend", "vendor-that-is-not-installed"),
        lambda value: value["request"].__setitem__("extra", 1),
    ),
)
def test_protocol_rejects_invalid_request_shapes(mutator) -> None:
    value = _request()
    mutator(value)

    with pytest.raises(WorkerProtocolError):
        encode_request_line(value)


def test_protocol_rejects_duplicate_request_ids() -> None:
    first = decode_request_line(encode_request_line(_request("same")))
    second = decode_request_line(encode_request_line(_request("same")))

    assert first.request_id == second.request_id
    with WorkerHarness(_test_command("ok")) as harness:
        harness.submit(first)
        with pytest.raises(WorkerProtocolError, match="duplicate request_id"):
            harness.submit(second)


def test_normal_close_reaps_the_leader_before_verifying_no_process_group_rows() -> None:
    with WorkerHarness(_test_command("ok")) as harness:
        response = harness.submit(decode_request_line(encode_request_line(_request("close"))))
        assert response.status == "OK"
        worker_pid = response.worker_pid
        assert worker_pid is not None

    assert process_group_rss_kib(response.pgid) == 0


def test_protocol_rejects_invalid_utf8_malformed_json_and_trailing_stdout() -> None:
    with pytest.raises(WorkerProtocolError, match="UTF-8"):
        decode_request_line(b"\xff\n")
    with pytest.raises(WorkerProtocolError, match="JSON"):
        decode_request_line(b"{not-json}\n")
    with pytest.raises(WorkerProtocolError, match="trailing"):
        decode_response_line(
            b'{"backend":"test","diagnostics":[],"evidence":null,"protocol":"'
            + BENCHMARK_PROTOCOL_V1.encode()
            + b'","request_id":"request-1","status":"OK"}\nextra\n',
            expected_request_id="request-1",
        )


def test_protocol_rejects_wrong_response_id_and_noncanonical_response() -> None:
    line = json.dumps(
        {
            "backend": "test",
            "diagnostics": [],
            "evidence": None,
            "protocol": BENCHMARK_PROTOCOL_V1,
            "request_id": "other",
            "status": "OK",
        },
        separators=(",", ":"),
    ).encode()
    with pytest.raises(WorkerProtocolError, match="request_id"):
        decode_response_line(line, expected_request_id="request-1")

    invalid = _request()
    invalid["request"]["problem"]["actions"][0]["min_quantity_lots"] = True
    with pytest.raises(WorkerProtocolError, match="canonical"):
        encode_request_line(invalid)


def test_handshake_is_strict_and_protocol_only() -> None:
    process = subprocess.Popen(
        _test_command("ok"),
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    handshake_line = process.stdout.readline()
    handshake = decode_handshake_line(handshake_line)
    assert handshake.protocol == BENCHMARK_PROTOCOL_V1
    assert handshake.backend == "test"
    assert handshake.pid == process.pid

    assert process.stdin is not None
    process.stdin.write(encode_request_line(_request()))
    process.stdin.flush()
    response_line = process.stdout.readline()
    response = decode_response_line(response_line, expected_request_id="request-1")
    assert response.status == "OK"
    process.stdin.close()
    assert process.wait(timeout=2) == 0
    assert process.stderr is not None
    assert process.stderr.read() == b""


def test_harness_rejects_handshake_with_a_pid_different_from_the_captured_process() -> None:
    code = (
        "import json,os,time; "
        "print(json.dumps({'backend':'test','pid':os.getpid()+1,'protocol':'open_trader.prediction_solver.protocol.v1','version':'1'}), flush=True); "
        "time.sleep(10)"
    )
    with WorkerHarness([sys.executable, "-c", code], request_timeout_ms=100) as harness:
        result = harness.submit(decode_request_line(encode_request_line(_request("pid-mismatch"))))

    assert result.status == "UNKNOWN"
    assert result.termination == "PROTOCOL_MISMATCH"
    assert result.cleanup_proven is True


def test_large_request_to_a_worker_that_never_reads_stdin_times_out_boundedly() -> None:
    code = (
        "import json,os,time; "
        "print(json.dumps({'backend':'test','pid':os.getpid(),'protocol':'open_trader.prediction_solver.protocol.v1','version':'1'}), flush=True); "
        "time.sleep(10)"
    )
    payload = _request("blocked", hard_time_limit_ms=100)
    payload["request"]["problem"]["problem_id"] = "x" * 200_000

    started = time.monotonic()
    with WorkerHarness([sys.executable, "-c", code], request_timeout_ms=500, cleanup_grace_seconds=0.1) as harness:
        result = harness.submit(decode_request_line(encode_request_line(payload)))
    elapsed = time.monotonic() - started

    assert result.status == "UNKNOWN"
    assert result.termination == "HARD_TIMEOUT"
    assert result.cleanup_proven is True
    assert elapsed < 1.0
    assert process_group_rss_kib(result.pgid) == 0


def test_write_stdin_checks_deadline_and_rss_on_successful_partial_writes(monkeypatch) -> None:
    import open_trader.prediction_solver_worker as worker_module

    stdin_read, stdin_write = os.pipe()
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()

    class Process:
        stdin = os.fdopen(stdin_write, "wb", buffering=0)
        stdout = os.fdopen(stdout_read, "rb", buffering=0)
        stderr = os.fdopen(stderr_read, "rb", buffering=0)

        @staticmethod
        def poll() -> None:
            return None

    process = Process()
    samples: list[bool] = []
    reader = _PipeReader(process, MAX_LINE_BYTES, on_read=lambda: samples.append(True))
    writes: list[bytes] = []
    monotonic_values = iter((0.0, 0.4, 0.8, 1.1))

    monkeypatch.setattr(worker_module.os, "write", lambda _fd, data: writes.append(bytes(data)) or min(2, len(data)))
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: next(monotonic_values))
    try:
        with pytest.raises(TimeoutError, match="writing request"):
            reader.write_stdin(b"abcdefgh", deadline=1.0)
    finally:
        reader.close()
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
        os.close(stdin_read)
        os.close(stdout_write)
        os.close(stderr_write)

    assert len(writes) == 3
    assert len(samples) == 3


def test_missing_worker_executable_finalizes_current_request_unknown() -> None:
    with WorkerHarness(["/definitely/missing/open-trader-worker"]) as harness:
        result = harness.submit(decode_request_line(encode_request_line(_request("missing"))))

    assert result.status == "UNKNOWN"
    assert result.termination == "CRASH"
    assert result.worker_pid is None
    assert result.cleanup_proven is True


def test_startup_handshake_timeout_is_reported_as_hard_timeout() -> None:
    with WorkerHarness(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        request_timeout_ms=500,
        startup_timeout_ms=50,
        cleanup_grace_seconds=0.1,
    ) as harness:
        result = harness.submit(decode_request_line(encode_request_line(_request("startup-timeout"))))

    assert result.status == "UNKNOWN"
    assert result.termination == "HARD_TIMEOUT"
    assert result.cleanup_proven is True


def test_malformed_startup_handshake_is_reported_as_protocol_mismatch() -> None:
    code = "import sys,time; sys.stdout.write('{malformed\\n'); sys.stdout.flush(); time.sleep(10)"
    with WorkerHarness([sys.executable, "-c", code], cleanup_grace_seconds=0.1) as harness:
        result = harness.submit(decode_request_line(encode_request_line(_request("startup-malformed"))))

    assert result.status == "UNKNOWN"
    assert result.termination == "PROTOCOL_MISMATCH"
    assert result.cleanup_proven is True


def test_harness_reuses_only_a_healthy_worker_and_rebuilds_after_hard_failure() -> None:
    with WorkerHarness(_test_command("ok")) as harness:
        first = harness.submit(decode_request_line(encode_request_line(_request("first"))))
        second = harness.submit(decode_request_line(encode_request_line(_request("second"))))

        assert first.status == "OK"
        assert second.status == "OK"
        assert first.worker_pid == second.worker_pid
        assert harness.start_count == 1
        assert harness.rebuild_count == 0

    with WorkerHarness(_test_command("exit17")) as harness:
        failed = harness.submit(decode_request_line(encode_request_line(_request("failed"))))
        assert failed.status == "UNKNOWN"
        assert failed.retried is False
        assert failed.termination == "CRASH"
        assert failed.cleanup_proven is True


def test_per_request_hard_deadline_caps_parent_wait() -> None:
    with WorkerHarness(_test_command("hang-child"), request_timeout_ms=1_000, cleanup_grace_seconds=0.1) as harness:
        started = time.monotonic()
        result = harness.submit(
            decode_request_line(
                encode_request_line(
                    _request("hard-deadline", soft_time_limit_ms=10, hard_time_limit_ms=50)
                )
            )
        )
        elapsed = time.monotonic() - started

    assert result.status == "UNKNOWN"
    assert result.termination == "HARD_TIMEOUT"
    assert result.cleanup_proven is True
    assert elapsed < 0.5


def test_protocol_mismatch_startup_is_an_unknown_outcome_not_a_context_exception() -> None:
    with WorkerHarness(_test_command("protocol-mismatch")) as harness:
        result = harness.submit(decode_request_line(encode_request_line(_request("mismatch"))))

    assert result.status == "UNKNOWN"
    assert result.termination == "PROTOCOL_MISMATCH"
    assert result.cleanup_proven is True


def test_rlimit_as_clamps_to_a_finite_hard_limit_without_raising_it(monkeypatch) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr("open_trader.prediction_solver_worker.resource.getrlimit", lambda _: (resource.RLIM_INFINITY, 8_192))
    monkeypatch.setattr(
        "open_trader.prediction_solver_worker.resource.setrlimit",
        lambda kind, limits: calls.append((kind, limits)),
    )

    from open_trader.prediction_solver_worker import _apply_rlimit_as

    _apply_rlimit_as(16_384)

    assert calls == [(resource.RLIMIT_AS, (8_192, 8_192))]


def test_rlimit_as_lowers_an_infinite_soft_limit_to_the_requested_cap(monkeypatch) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        "open_trader.prediction_solver_worker.resource.getrlimit",
        lambda _: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(
        "open_trader.prediction_solver_worker.resource.setrlimit",
        lambda kind, limits: calls.append((kind, limits)),
    )

    from open_trader.prediction_solver_worker import _apply_rlimit_as

    _apply_rlimit_as(16_384)

    assert calls == [(resource.RLIMIT_AS, (16_384, resource.RLIM_INFINITY))]


def test_rlimit_as_applies_before_test_mode_work_and_uses_the_stricter_cli_limit(monkeypatch) -> None:
    events: list[tuple[str, int | str]] = []
    request = decode_request_line(encode_request_line(_request("rlimit")))

    class _Input:
        buffer = io.BytesIO(encode_request_line(request))

    class _Output:
        buffer = io.BytesIO()

    monkeypatch.setattr("open_trader.prediction_solver_worker.sys.stdin", _Input())
    monkeypatch.setattr("open_trader.prediction_solver_worker.sys.stdout", _Output())
    monkeypatch.setattr(
        "open_trader.prediction_solver_worker._apply_rlimit_as",
        lambda limit: events.append(("rlimit", limit)),
    )
    monkeypatch.setattr(
        "open_trader.prediction_solver_worker._test_response",
        lambda _request, _mode: events.append(("work", "test")) or None,
    )

    from open_trader.prediction_solver_worker import _run_worker

    assert _run_worker(
        SimpleNamespace(
            backend=None,
            memory_limit_bytes=32,
            test_mode="ok",
        )
    ) == 0
    assert events == [("rlimit", 32), ("rlimit", 32), ("work", "test")]


@pytest.mark.parametrize(
    "mode",
    (
        "cooperative-timeout",
        "hang-child",
        "exit17",
        "malformed-json",
        "truncated-json",
        "protocol-mismatch",
        "memory-growth",
        "absent-certificate",
        "corrupt-certificate",
        "checker-failure",
    ),
)
def test_deterministic_protocol_failures_finalize_unknown_without_retry(mode: str) -> None:
    with WorkerHarness(_test_command(mode)) as harness:
        memory_limit = 1024 * 1024 * 1024 * 1024
        result = harness.submit(
            decode_request_line(encode_request_line(_request("failed", memory_limit_bytes=memory_limit)))
        )

        assert result.status == "UNKNOWN"
        assert result.retried is False
        if mode == "memory-growth":
            assert result.termination == "MEMORY_LIMIT"
        assert harness.request_count == 1
        assert result.worker_pid is not None
        assert result.cleanup_proven is True
        assert process_group_rss_kib(result.pgid) == 0
        assert harness.start_count == 1
        assert harness.rebuild_count == 0

        harness.command = _test_command("ok")
        recovered = harness.submit(decode_request_line(encode_request_line(_request("recovered"))))
        assert recovered.status == "OK"
        assert recovered.retried is False
        assert recovered.worker_pid != result.worker_pid
        assert harness.start_count == 2
        assert harness.rebuild_count == 1


def test_twenty_serial_hard_failures_leave_no_monotonic_orphan_rss() -> None:
    rss: list[int] = []
    with WorkerHarness(_test_command("hang-child"), request_timeout_ms=250) as harness:
        for index in range(20):
            result = harness.submit(decode_request_line(encode_request_line(_request(f"request-{index}"))))
            assert result.status == "UNKNOWN"
            assert result.cleanup_proven is True
            rss.append(process_group_rss_kib(result.pgid))

    assert rss == [0] * 20
    assert harness.start_count == 20
    assert harness.rebuild_count <= 20


def test_group_rss_parser_uses_exact_ps_columns(monkeypatch) -> None:
    class Result:
        returncode = 0
        stdout = " 10  200  12\n 11 200  8\n 12  201  100\n"
        stderr = ""

    monkeypatch.setattr("open_trader.prediction_solver_worker.subprocess.run", lambda *args, **kwargs: Result())
    assert process_group_rss_kib(200) == 20
    assert process_group_rss_kib(201) == 100


def test_peak_rss_is_sampled_while_bounded_stdout_is_being_read(monkeypatch) -> None:
    sampled_during_read: list[bool] = []
    original = sys.modules["open_trader.prediction_solver_worker"]._WorkerProcess.sample_rss

    def sample(worker) -> None:
        sampled_during_read.append(bool(worker.reader.stdout_buffer))
        original(worker)

    monkeypatch.setattr("open_trader.prediction_solver_worker._WorkerProcess.sample_rss", sample)
    with WorkerHarness(_test_command("ok")) as harness:
        outcome = harness.submit(decode_request_line(encode_request_line(_request("rss"))))

    assert outcome.status == "OK"
    assert any(sampled_during_read)


def test_peak_rss_sampling_ticks_during_a_silent_solver_wait(monkeypatch) -> None:
    protocol = BENCHMARK_PROTOCOL_V1
    code = (
        "import json,sys,time; "
        f"print(json.dumps({{'backend':'test','pid':__import__('os').getpid(),'protocol':'{protocol}','version':'1'}}), flush=True); "
        "request=json.loads(sys.stdin.readline()); time.sleep(.2); "
        f"print(json.dumps({{'backend':'test','diagnostics':[],'evidence':None,'protocol':'{protocol}','request_id':request['request_id'],'status':'OK'}}), flush=True); "
        "time.sleep(10)"
    )
    started = time.monotonic()
    early_samples: list[bool] = []
    original = sys.modules["open_trader.prediction_solver_worker"]._WorkerProcess.sample_rss

    def sample(worker) -> None:
        if time.monotonic() - started < 0.15 and not worker.reader.stdout_buffer:
            early_samples.append(True)
        original(worker)

    monkeypatch.setattr("open_trader.prediction_solver_worker._WorkerProcess.sample_rss", sample)
    with WorkerHarness([sys.executable, "-c", code], request_timeout_ms=500, cleanup_grace_seconds=0.1) as harness:
        outcome = harness.submit(decode_request_line(encode_request_line(_request("silent"))))

    assert outcome.status == "OK"
    assert early_samples


def test_changed_request_memory_limit_rebuilds_worker_before_dispatch() -> None:
    with WorkerHarness(_test_command("ok")) as harness:
        first = harness.submit(
            decode_request_line(encode_request_line(_request("wide", memory_limit_bytes=2 * 1024 * 1024 * 1024 * 1024)))
        )
        second = harness.submit(
            decode_request_line(encode_request_line(_request("narrow", memory_limit_bytes=1024 * 1024 * 1024 * 1024)))
        )

    assert first.status == "OK"
    assert second.status == "OK"
    assert first.worker_pid != second.worker_pid
    assert harness.start_count == 2
    assert harness.rebuild_count == 1


def test_cleanup_failure_poisons_harness_and_blocks_later_process_start(monkeypatch) -> None:
    import open_trader.prediction_solver_worker as worker_module

    popen_calls: list[object] = []
    original_popen = worker_module.subprocess.Popen

    def recording_popen(*args, **kwargs):
        if kwargs.get("start_new_session") is True:
            popen_calls.append(args[0] if args else kwargs.get("args"))
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(worker_module.subprocess, "Popen", recording_popen)
    harness = WorkerHarness(_test_command("ok"))
    original_terminate = harness._terminate
    try:
        first = harness.submit(decode_request_line(encode_request_line(_request("poison-a"))))
        monkeypatch.setattr(harness, "_terminate", lambda worker: original_terminate(worker) and False)
        failed = harness.submit(
            decode_request_line(
                encode_request_line(_request("poison-b", memory_limit_bytes=2 * 1024 * 1024 * 1024 * 1024))
            )
        )
        blocked = harness.submit(
            decode_request_line(
                encode_request_line(_request("poison-c", memory_limit_bytes=3 * 1024 * 1024 * 1024 * 1024))
            )
        )
    finally:
        harness._terminate = original_terminate
        harness.close()

    assert first.status == "OK"
    assert failed.status == "UNKNOWN"
    assert failed.termination == "CLEANUP_UNPROVEN"
    assert failed.cleanup_proven is False
    assert failed.worker_pid == first.worker_pid
    assert failed.pgid == first.pgid
    assert failed.peak_rss_kib == first.peak_rss_kib
    assert blocked.status == "UNKNOWN"
    assert blocked.termination == "CLEANUP_UNPROVEN"
    assert blocked.cleanup_proven is False
    assert blocked.worker_pid == first.worker_pid
    assert blocked.pgid == first.pgid
    assert blocked.peak_rss_kib == first.peak_rss_kib
    assert len(popen_calls) == 1


def test_unproven_hard_timeout_reports_cleanup_unproven_and_poisoned_next_request(monkeypatch) -> None:
    import open_trader.prediction_solver_worker as worker_module

    popen_calls: list[object] = []
    original_popen = worker_module.subprocess.Popen

    def recording_popen(*args, **kwargs):
        if kwargs.get("start_new_session") is True:
            popen_calls.append(args[0] if args else kwargs.get("args"))
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(worker_module.subprocess, "Popen", recording_popen)
    harness = WorkerHarness(_test_command("hang-child"), request_timeout_ms=100, cleanup_grace_seconds=0.1)
    original_terminate = harness._terminate
    try:
        monkeypatch.setattr(harness, "_terminate", lambda worker: original_terminate(worker) and False)
        failed = harness.submit(
            decode_request_line(
                encode_request_line(_request("unproven-timeout", soft_time_limit_ms=10, hard_time_limit_ms=50))
            )
        )
        blocked = harness.submit(decode_request_line(encode_request_line(_request("after-unproven"))))
    finally:
        harness._terminate = original_terminate
        harness.close()

    assert failed.status == "UNKNOWN"
    assert failed.termination == "CLEANUP_UNPROVEN"
    assert failed.cleanup_proven is False
    assert blocked.status == "UNKNOWN"
    assert blocked.termination == "CLEANUP_UNPROVEN"
    assert blocked.cleanup_proven is False
    assert blocked.worker_pid == failed.worker_pid
    assert blocked.pgid == failed.pgid
    assert blocked.peak_rss_kib == failed.peak_rss_kib
    assert len(popen_calls) == 1


def test_reused_worker_resets_peak_rss_for_each_request(monkeypatch) -> None:
    request_phases: dict[int, int] = {}

    original_begin_request = sys.modules["open_trader.prediction_solver_worker"]._WorkerProcess.begin_request

    def begin_request(worker) -> None:
        key = id(worker)
        request_phases[key] = request_phases.get(key, 0) + 1
        original_begin_request(worker)

    def deterministic_sample(worker) -> None:
        current = 100 if request_phases.get(id(worker), 0) == 1 else 10
        worker.peak_rss_kib = max(worker.peak_rss_kib, current)

    monkeypatch.setattr("open_trader.prediction_solver_worker._WorkerProcess.begin_request", begin_request)
    monkeypatch.setattr("open_trader.prediction_solver_worker._WorkerProcess.sample_rss", deterministic_sample)
    with WorkerHarness(_test_command("ok")) as harness:
        first = harness.submit(decode_request_line(encode_request_line(_request("rss-a"))))
        second = harness.submit(decode_request_line(encode_request_line(_request("rss-b"))))

    assert first.status == "OK"
    assert first.peak_rss_kib == 100
    assert second.status == "OK"
    assert second.peak_rss_kib == 10


def test_line_bound_is_enforced_before_json_decode() -> None:
    with pytest.raises(WorkerProtocolError, match="line limit"):
        decode_request_line(b"{" + b"x" * MAX_LINE_BYTES + b"}\n")
