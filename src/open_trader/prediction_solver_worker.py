"""Small line-oriented subprocess boundary for native solver adapters.

The worker deliberately owns no long-lived application state.  A parent may
reuse it only after a complete, exact response; failures are isolated by
terminating the worker's process group before the next request is started.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
import resource
import selectors
import signal
import subprocess
import sys
import time
from typing import Callable

from open_trader.prediction_n_leg import ModelDecodeError, OracleRequest, canonical_payload, request_from_payload
from open_trader.prediction_solver import (
    BENCHMARK_PROTOCOL_V1,
    BackendResult,
    BenchmarkLimits,
    LinearModel,
    SolverBackend,
    solve_with_constraint_generation,
)


MAX_LINE_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 64 * 1024
WORKER_VERSION = "1"
SUPPORTED_BACKENDS = frozenset({"highs", "scip", "cp_sat", "test"})
NATIVE_BACKENDS = frozenset({"highs", "scip", "cp_sat"})
WORKER_PHASE_NAMES = (
    "backend",
    "first_qualified",
    "optimal",
    "certificate_generation",
    "certificate_completion",
    "certificate_check",
    "serialization",
)


class WorkerProtocolError(ValueError):
    """The worker wire contract was violated."""


class WorkerCleanupError(RuntimeError):
    """A worker process group could not be proven dead."""


class WorkerStartupError(WorkerProtocolError):
    """The startup handshake failed after the process was isolated."""

    def __init__(
        self,
        message: str,
        *,
        termination: str,
        worker_pid: int | None,
        pgid: int | None,
        peak_rss_kib: int,
        cleanup_proven: bool,
    ) -> None:
        super().__init__(message)
        self.termination = termination
        self.worker_pid = worker_pid
        self.pgid = pgid
        self.peak_rss_kib = peak_rss_kib
        self.cleanup_proven = cleanup_proven


def _reject_constant(value: str) -> None:
    raise WorkerProtocolError(f"non-standard JSON constant: {value}")


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json_line(raw: bytes | str) -> object:
    if isinstance(raw, str):
        payload = raw.encode("utf-8")
    elif isinstance(raw, bytes):
        payload = raw
    else:
        raise WorkerProtocolError("line must be bytes or text")
    if len(payload) > MAX_LINE_BYTES:
        raise WorkerProtocolError("line limit exceeded")
    if payload.endswith(b"\n"):
        payload = payload[:-1]
        if payload.endswith(b"\r"):
            payload = payload[:-1]
    if b"\n" in payload or b"\r" in payload:
        raise WorkerProtocolError("trailing stdout bytes")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WorkerProtocolError("invalid UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, WorkerProtocolError):
            raise
        raise WorkerProtocolError("invalid JSON") from exc


def _object(value: object, name: str, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys or not all(isinstance(key, str) for key in value):
        raise WorkerProtocolError(f"{name} must contain exactly {sorted(keys)}")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkerProtocolError(f"{name} must be a non-empty string")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WorkerProtocolError(f"{name} must be a positive integer")
    return value


def _termination_after_cleanup(termination: str, cleanup_proven: bool) -> str:
    return termination if cleanup_proven else "CLEANUP_UNPROVEN"


def _limits_from_payload(payload: object) -> BenchmarkLimits:
    value = _object(
        payload,
        "limits",
        {"soft_time_limit_ms", "hard_time_limit_ms", "memory_limit_bytes", "max_constraint_generation_rounds"},
    )
    try:
        return BenchmarkLimits(
            _positive_int(value["soft_time_limit_ms"], "soft_time_limit_ms"),
            _positive_int(value["hard_time_limit_ms"], "hard_time_limit_ms"),
            _positive_int(value["memory_limit_bytes"], "memory_limit_bytes"),
            _positive_int(value["max_constraint_generation_rounds"], "max_constraint_generation_rounds"),
        )
    except ValueError as exc:
        raise WorkerProtocolError(str(exc)) from exc


def _limits_to_payload(limits: BenchmarkLimits) -> dict[str, int]:
    if not isinstance(limits, BenchmarkLimits):
        raise WorkerProtocolError("limits must be BenchmarkLimits")
    return {
        "hard_time_limit_ms": limits.hard_time_limit_ms,
        "max_constraint_generation_rounds": limits.max_constraint_generation_rounds,
        "memory_limit_bytes": limits.memory_limit_bytes,
        "soft_time_limit_ms": limits.soft_time_limit_ms,
    }


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    request_id: str
    backend: str
    request: OracleRequest
    limits: BenchmarkLimits

    def __post_init__(self) -> None:
        _string(self.request_id, "request_id")
        if self.backend not in SUPPORTED_BACKENDS:
            raise WorkerProtocolError(f"unsupported backend: {self.backend}")
        if not isinstance(self.request, OracleRequest):
            raise WorkerProtocolError("request must be a canonical OracleRequest")
        if not isinstance(self.limits, BenchmarkLimits):
            raise WorkerProtocolError("limits must be BenchmarkLimits")

    @classmethod
    def from_payload(cls, payload: object) -> "WorkerRequest":
        value = _object(payload, "request envelope", {"backend", "limits", "protocol", "request", "request_id"})
        protocol = _string(value["protocol"], "protocol")
        if protocol != BENCHMARK_PROTOCOL_V1:
            raise WorkerProtocolError(f"unsupported protocol: {protocol}")
        backend = _string(value["backend"], "backend")
        if backend not in SUPPORTED_BACKENDS:
            raise WorkerProtocolError(f"unsupported backend: {backend}")
        request_id = _string(value["request_id"], "request_id")
        limits = _limits_from_payload(value["limits"])
        try:
            request = request_from_payload(value["request"] if isinstance(value["request"], Mapping) else {})
        except (ModelDecodeError, TypeError, ValueError) as exc:
            raise WorkerProtocolError(f"malformed canonical request: {exc}") from exc
        return cls(request_id, backend, request, limits)

    def to_payload(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "limits": _limits_to_payload(self.limits),
            "protocol": BENCHMARK_PROTOCOL_V1,
            "request": canonical_payload(self.request),
            "request_id": self.request_id,
        }


@dataclass(frozen=True, slots=True)
class WorkerHandshake:
    protocol: str
    backend: str
    version: str
    pid: int


@dataclass(frozen=True, slots=True)
class WorkerResponse:
    protocol: str
    backend: str
    request_id: str
    status: str
    evidence: Mapping[str, object] | None
    phase_timings_ns: Mapping[str, int]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    request_id: str
    status: str
    termination: str
    worker_pid: int | None
    pgid: int | None
    peak_rss_kib: int
    retried: bool
    cleanup_proven: bool
    response: WorkerResponse | None = None


def decode_handshake_line(raw: bytes | str) -> WorkerHandshake:
    value = _object(_decode_json_line(raw), "handshake", {"backend", "pid", "protocol", "version"})
    protocol = _string(value["protocol"], "protocol")
    if protocol != BENCHMARK_PROTOCOL_V1:
        raise WorkerProtocolError(f"unsupported protocol: {protocol}")
    backend = _string(value["backend"], "backend")
    if backend not in SUPPORTED_BACKENDS:
        raise WorkerProtocolError(f"unsupported backend: {backend}")
    version = _string(value["version"], "version")
    pid = _positive_int(value["pid"], "pid")
    return WorkerHandshake(protocol, backend, version, pid)


def encode_request_line(request: WorkerRequest | Mapping[str, object]) -> bytes:
    if isinstance(request, WorkerRequest):
        value = request.to_payload()
    elif isinstance(request, Mapping):
        value = dict(request)
    else:
        raise WorkerProtocolError("invalid worker request")
    canonical = WorkerRequest.from_payload(value).to_payload()
    return _encode_line(canonical)


def decode_request_line(raw: bytes | str) -> WorkerRequest:
    return WorkerRequest.from_payload(_decode_json_line(raw))


def encode_response_line(response: WorkerResponse | Mapping[str, object]) -> bytes:
    if isinstance(response, WorkerResponse):
        value: dict[str, object] = {
            "backend": response.backend,
            "diagnostics": list(response.diagnostics),
            "evidence": None if response.evidence is None else dict(response.evidence),
            "phase_timings_ns": dict(response.phase_timings_ns),
            "protocol": response.protocol,
            "request_id": response.request_id,
            "status": response.status,
        }
    elif isinstance(response, Mapping):
        value = dict(response)
    else:
        raise WorkerProtocolError("invalid worker response")
    decoded = decode_response_line(_encode_line(value))
    return _encode_line(
        {
            "backend": decoded.backend,
            "diagnostics": list(decoded.diagnostics),
            "evidence": None if decoded.evidence is None else dict(decoded.evidence),
            "phase_timings_ns": dict(decoded.phase_timings_ns),
            "protocol": decoded.protocol,
            "request_id": decoded.request_id,
            "status": decoded.status,
        }
    )


def decode_response_line(raw: bytes | str, *, expected_request_id: str | None = None, expected_backend: str | None = None) -> WorkerResponse:
    value = _object(_decode_json_line(raw), "response", {"backend", "diagnostics", "evidence", "phase_timings_ns", "protocol", "request_id", "status"})
    protocol = _string(value["protocol"], "protocol")
    if protocol != BENCHMARK_PROTOCOL_V1:
        raise WorkerProtocolError(f"unsupported protocol: {protocol}")
    backend = _string(value["backend"], "backend")
    if backend not in SUPPORTED_BACKENDS:
        raise WorkerProtocolError(f"unsupported backend: {backend}")
    request_id = _string(value["request_id"], "request_id")
    if expected_request_id is not None and request_id != expected_request_id:
        raise WorkerProtocolError("response request_id does not match request")
    if expected_backend is not None and backend != expected_backend:
        raise WorkerProtocolError("response backend does not match worker")
    status = _string(value["status"], "status")
    if status not in {"OK", "UNKNOWN"}:
        raise WorkerProtocolError(f"unsupported response status: {status}")
    raw_diagnostics = value["diagnostics"]
    if not isinstance(raw_diagnostics, list) or not all(isinstance(item, str) for item in raw_diagnostics):
        raise WorkerProtocolError("diagnostics must be an array of strings")
    evidence = value["evidence"]
    if evidence is not None and not isinstance(evidence, Mapping):
        raise WorkerProtocolError("evidence must be an object or null")
    timings = _phase_timings_from_payload(value["phase_timings_ns"])
    return WorkerResponse(protocol, backend, request_id, status, None if evidence is None else dict(evidence), timings, tuple(raw_diagnostics))


def _phase_timings_from_payload(payload: object) -> dict[str, int]:
    if not isinstance(payload, Mapping) or set(payload) != set(WORKER_PHASE_NAMES) or not all(isinstance(key, str) for key in payload):
        raise WorkerProtocolError(f"phase_timings_ns must contain exactly {sorted(WORKER_PHASE_NAMES)}")
    timings = {}
    for name in WORKER_PHASE_NAMES:
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WorkerProtocolError(f"phase_timings_ns.{name} must be a non-negative integer")
        timings[name] = value
    return timings


def _zero_phase_timings() -> dict[str, int]:
    return {name: 0 for name in WORKER_PHASE_NAMES}


class _TimingBackend:
    def __init__(self, backend: SolverBackend) -> None:
        self.backend = backend
        self.name = backend.name
        self.version = backend.version
        self.solve_ns = 0

    def solve(self, model: LinearModel, *, time_limit_ms: int) -> BackendResult:
        result = self.backend.solve(model, time_limit_ms=time_limit_ms)
        self.solve_ns += result.solve_ns
        return result


def _encode_line(value: object) -> bytes:
    try:
        line = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise WorkerProtocolError("value is not strict JSON") from exc
    if len(line) > MAX_LINE_BYTES:
        raise WorkerProtocolError("line limit exceeded")
    return line


def _ps_group_rows(pgid: int) -> dict[int, int]:
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 0:
        raise WorkerCleanupError("invalid process group ID")
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,rss="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise WorkerCleanupError("unable to inspect process groups") from exc
    if completed.returncode != 0:
        raise WorkerCleanupError("process-group inspection failed")
    output = completed.stdout.decode("utf-8", "strict") if isinstance(completed.stdout, bytes) else completed.stdout
    rows: dict[int, int] = {}
    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 3 or any(not field.isdigit() for field in fields):
            raise WorkerCleanupError("corrupt process-group inspection output")
        pid, row_pgid, rss = (int(field) for field in fields)
        if row_pgid == pgid:
            rows[pid] = rss
    return rows


def process_group_rss_kib(pgid: int) -> int:
    return sum(_ps_group_rows(pgid).values())


def _apply_rlimit_as(limit_bytes: int) -> None:
    if not hasattr(resource, "RLIMIT_AS"):
        return
    _positive_int(limit_bytes, "memory_limit_bytes")
    current_soft, current_hard = resource.getrlimit(resource.RLIMIT_AS)
    hard_cap = current_hard
    target_soft = min(limit_bytes, hard_cap)
    if current_soft != resource.RLIM_INFINITY:
        target_soft = min(target_soft, current_soft)
    if current_soft != target_soft:
        resource.setrlimit(resource.RLIMIT_AS, (target_soft, current_hard))


class _PipeReader:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        max_line_bytes: int,
        on_read: Callable[[], None] | None = None,
    ) -> None:
        if process.stdout is None or process.stderr is None:
            raise WorkerProtocolError("worker pipes must be captured")
        self.process = process
        self.max_line_bytes = max_line_bytes
        self.on_read = on_read
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        self.stdout_buffer = bytearray()
        self.stderr_buffer = bytearray()

    def close(self) -> None:
        try:
            self.selector.close()
        except Exception:
            pass

    def _read_ready(self, key: selectors.SelectorKey) -> bytes:
        try:
            data = os.read(key.fd, 65536)
        except OSError:
            return b""
        if key.data == "stdout":
            self.stdout_buffer.extend(data)
            if len(self.stdout_buffer) > self.max_line_bytes:
                raise WorkerProtocolError("line limit exceeded")
        elif data:
            if len(self.stderr_buffer) < MAX_DIAGNOSTIC_BYTES:
                self.stderr_buffer.extend(data[: MAX_DIAGNOSTIC_BYTES - len(self.stderr_buffer)])
        if data and self.on_read is not None:
            self.on_read()
        return data

    def read_stdout_line(self, deadline: float) -> bytes:
        while True:
            separator = self.stdout_buffer.find(b"\n")
            if separator >= 0:
                line = bytes(self.stdout_buffer[: separator + 1])
                del self.stdout_buffer[: separator + 1]
                if self.stdout_buffer:
                    raise WorkerProtocolError("trailing stdout bytes")
                return line
            if self.process.poll() is not None:
                if self.stdout_buffer:
                    raise WorkerProtocolError("truncated JSON line")
                raise WorkerProtocolError("worker exited before response")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("worker deadline exceeded")
            events = self.selector.select(min(remaining, 0.05))
            if not events:
                if self.on_read is not None:
                    self.on_read()
                continue
            for key, _ in events:
                self._read_ready(key)

    def write_stdin(self, payload: bytes, deadline: float) -> None:
        if self.process.stdin is None:
            raise WorkerProtocolError("worker stdin is unavailable")
        descriptor = self.process.stdin.fileno()
        os.set_blocking(descriptor, False)
        offset = 0
        self.selector.register(self.process.stdin, selectors.EVENT_WRITE, "stdin")
        try:
            while offset < len(payload):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("worker deadline exceeded while writing request")
                if self.on_read is not None:
                    self.on_read()
                if self.process.poll() is not None:
                    raise BrokenPipeError("worker exited while request was being written")
                try:
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise BrokenPipeError("worker stdin accepted no bytes")
                    offset += written
                    continue
                except BlockingIOError:
                    pass
                events = self.selector.select(min(remaining, 0.05))
                for key, _ in events:
                    if key.data != "stdin":
                        self._read_ready(key)
        finally:
            try:
                self.selector.unregister(self.process.stdin)
            except (KeyError, ValueError):
                pass

    def assert_no_trailing_stdout(self) -> None:
        events = self.selector.select(0)
        for key, _ in events:
            self._read_ready(key)
        if self.stdout_buffer:
            raise WorkerProtocolError("trailing stdout bytes")


class WorkerHarness:
    """Start, reuse, and fail-closed native solver workers."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        request_timeout_ms: int = 2_000,
        startup_timeout_ms: int = 2_000,
        cleanup_grace_seconds: float = 0.5,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("command must be a non-empty sequence of strings")
        self.command = list(command)
        self.request_timeout_ms = _positive_int(request_timeout_ms, "request_timeout_ms")
        self.startup_timeout_ms = _positive_int(startup_timeout_ms, "startup_timeout_ms")
        if cleanup_grace_seconds <= 0:
            raise ValueError("cleanup_grace_seconds must be positive")
        self.cleanup_grace_seconds = cleanup_grace_seconds
        self.env = None if env is None else dict(env)
        self._worker: _WorkerProcess | None = None
        self._request_ids: set[str] = set()
        self.request_count = 0
        self.start_count = 0
        self.rebuild_count = 0
        self._poisoned = False
        self._poisoned_evidence: tuple[int | None, int | None, int] | None = None

    def __enter__(self) -> "WorkerHarness":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _start(self, expected_backend: str, deadline: float | None = None) -> "_WorkerProcess":
        if self.start_count:
            self.rebuild_count += 1
        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                start_new_session=True,
            )
        except OSError as exc:
            raise WorkerStartupError(
                f"worker process could not start: {exc}",
                termination="CRASH",
                worker_pid=None,
                pgid=None,
                peak_rss_kib=0,
                cleanup_proven=True,
            ) from exc
        worker = _WorkerProcess(process, max_line_bytes=MAX_LINE_BYTES)
        self.start_count += 1
        try:
            startup_deadline = time.monotonic() + self.startup_timeout_ms / 1000
            if deadline is not None:
                startup_deadline = min(startup_deadline, deadline)
            handshake = worker.reader.read_stdout_line(startup_deadline)
            decoded = decode_handshake_line(handshake)
            if decoded.backend != expected_backend:
                raise WorkerProtocolError("handshake backend does not match request")
            worker.handshake = decoded
            worker.reader.assert_no_trailing_stdout()
        except Exception as exc:
            proven = self._terminate_checked(worker)
            raise WorkerStartupError(
                "worker startup handshake failed",
                termination=_termination_after_cleanup(
                    "HARD_TIMEOUT" if isinstance(exc, TimeoutError) else "PROTOCOL_MISMATCH",
                    proven,
                ),
                worker_pid=worker.process.pid,
                pgid=worker.pgid,
                peak_rss_kib=worker.peak_rss_kib,
                cleanup_proven=proven,
            )
        self._worker = worker
        return worker

    def submit(self, request: WorkerRequest) -> WorkerOutcome:
        if not isinstance(request, WorkerRequest):
            raise WorkerProtocolError("submit requires WorkerRequest")
        if request.request_id in self._request_ids:
            raise WorkerProtocolError(f"duplicate request_id: {request.request_id}")
        request_line = encode_request_line(request)
        deadline = time.monotonic() + min(self.request_timeout_ms, request.limits.hard_time_limit_ms) / 1000
        self._request_ids.add(request.request_id)
        self.request_count += 1
        if self._poisoned:
            worker_pid, pgid, peak_rss_kib = self._poisoned_evidence or (None, None, 0)
            return WorkerOutcome(
                request.request_id,
                "UNKNOWN",
                "CLEANUP_UNPROVEN",
                worker_pid,
                pgid,
                peak_rss_kib,
                False,
                False,
            )
        try:
            worker = self._worker
            if worker is not None and worker.memory_limit_bytes != request.limits.memory_limit_bytes:
                old_worker = worker
                cleanup_proven = self._terminate_checked(old_worker)
                if not cleanup_proven:
                    self._worker = None
                    return WorkerOutcome(
                        request.request_id,
                        "UNKNOWN",
                        _termination_after_cleanup("PROTOCOL_MISMATCH", cleanup_proven),
                        old_worker.process.pid,
                        old_worker.pgid,
                        old_worker.peak_rss_kib,
                        False,
                        False,
                    )
                self._worker = None
                worker = None
            worker = worker or self._start(request.backend, deadline)
            worker.memory_limit_bytes = request.limits.memory_limit_bytes
        except WorkerStartupError as exc:
            return WorkerOutcome(
                request.request_id,
                "UNKNOWN",
                exc.termination,
                exc.worker_pid,
                exc.pgid,
                exc.peak_rss_kib,
                False,
                exc.cleanup_proven,
            )
        process = worker.process
        pgid = worker.pgid
        try:
            worker.begin_request()
            worker.reader.write_stdin(request_line, deadline)
            response_line = worker.reader.read_stdout_line(deadline)
            response = decode_response_line(response_line, expected_request_id=request.request_id, expected_backend=request.backend)
            worker.sample_rss()
            worker.reader.assert_no_trailing_stdout()
            if response.status != "OK":
                cleanup_proven = self._terminate_checked(worker)
                self._worker = None
                return WorkerOutcome(
                    request.request_id,
                    "UNKNOWN",
                    _termination_after_cleanup(
                        "SOFT_TIMEOUT" if any("timeout" in item for item in response.diagnostics) else "UNKNOWN",
                        cleanup_proven,
                    ),
                    process.pid,
                    pgid,
                    worker.peak_rss_kib,
                    False,
                    cleanup_proven,
                    response,
                )
            return WorkerOutcome(request.request_id, "OK", "COMPLETED", process.pid, pgid, worker.peak_rss_kib, False, True, response)
        except TimeoutError:
            termination = "INVALID_OUTPUT" if worker.reader.stdout_buffer else "HARD_TIMEOUT"
            cleanup_proven = self._terminate_checked(worker)
            self._worker = None
            return WorkerOutcome(
                request.request_id,
                "UNKNOWN",
                _termination_after_cleanup(termination, cleanup_proven),
                process.pid,
                pgid,
                worker.peak_rss_kib,
                False,
                cleanup_proven,
            )
        except (BrokenPipeError, OSError, WorkerProtocolError, WorkerCleanupError, ValueError):
            returncode = process.poll()
            cleanup_proven = self._terminate_checked(worker)
            self._worker = None
            if returncode == 3:
                termination = "MEMORY_LIMIT"
            elif returncode is not None and returncode not in {0, 2}:
                termination = "CRASH"
            elif returncode == 0:
                termination = "INVALID_OUTPUT"
            else:
                termination = "PROTOCOL_MISMATCH"
            return WorkerOutcome(
                request.request_id,
                "UNKNOWN",
                _termination_after_cleanup(termination, cleanup_proven),
                process.pid,
                pgid,
                worker.peak_rss_kib,
                False,
                cleanup_proven,
            )

    def _poison_after_cleanup(self, worker: "_WorkerProcess") -> None:
        if not self._poisoned:
            self._poisoned = True
            self._poisoned_evidence = (worker.process.pid, worker.pgid, worker.peak_rss_kib)

    def _terminate_checked(self, worker: "_WorkerProcess") -> bool:
        proven = self._terminate(worker)
        if not proven:
            self._poison_after_cleanup(worker)
        return proven

    def _terminate(self, worker: "_WorkerProcess") -> bool:
        process = worker.process
        pgid = worker.pgid
        try:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + self.cleanup_grace_seconds
            while time.monotonic() < deadline:
                # Reap an exited leader before treating its process-table
                # zombie row as a surviving member of the group.
                try:
                    process.wait(timeout=0)
                except subprocess.TimeoutExpired:
                    pass
                worker.sample_rss()
                if not _ps_group_rows(pgid):
                    break
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            try:
                process.wait(timeout=0)
            except subprocess.TimeoutExpired:
                pass
            survivors = _ps_group_rows(pgid)
            if survivors:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                kill_deadline = time.monotonic() + self.cleanup_grace_seconds
                while time.monotonic() < kill_deadline:
                    try:
                        process.wait(timeout=0)
                    except subprocess.TimeoutExpired:
                        pass
                    if not _ps_group_rows(pgid):
                        break
                    time.sleep(0.02)
            try:
                process.wait(timeout=max(0.1, self.cleanup_grace_seconds))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=max(0.1, self.cleanup_grace_seconds))
            proven = not _ps_group_rows(pgid)
        except (OSError, subprocess.TimeoutExpired, WorkerCleanupError):
            proven = False
        finally:
            worker.close()
        return proven

    def close(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            if not self._terminate_checked(worker):
                raise WorkerCleanupError("worker cleanup could not be proven")


class _WorkerProcess:
    def __init__(self, process: subprocess.Popen[bytes], *, max_line_bytes: int) -> None:
        self.process = process
        self.pgid = process.pid
        self.reader = _PipeReader(process, max_line_bytes, on_read=self.sample_rss)
        self.handshake: WorkerHandshake | None = None
        self.peak_rss_kib = 0
        self.memory_limit_bytes: int | None = None

    def begin_request(self) -> None:
        self.peak_rss_kib = 0
        self.sample_rss()

    def sample_rss(self) -> None:
        current = process_group_rss_kib(self.pgid)
        if current > self.peak_rss_kib:
            self.peak_rss_kib = current

    def close(self) -> None:
        self.reader.close()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _test_response(request: WorkerRequest, mode: str) -> WorkerResponse | None:
    if mode == "ok":
        return WorkerResponse(BENCHMARK_PROTOCOL_V1, request.backend, request.request_id, "OK", {"request_id": request.request_id, "mode": mode}, _zero_phase_timings(), ())
    if mode == "cooperative-timeout":
        time.sleep(request.limits.soft_time_limit_ms / 1000 + 0.02)
        return WorkerResponse(BENCHMARK_PROTOCOL_V1, request.backend, request.request_id, "UNKNOWN", None, _zero_phase_timings(), ("cooperative timeout",))
    if mode == "hang-child":
        child = subprocess.Popen(
            [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
            start_new_session=False,
        )
        print(f"test child pid={child.pid}", file=sys.stderr, flush=True)
        while True:
            time.sleep(1)
    if mode == "exit17":
        os._exit(17)
    if mode == "malformed-json":
        sys.stdout.write("{malformed\n")
        sys.stdout.flush()
        return None
    if mode == "truncated-json":
        sys.stdout.write('{"protocol":')
        sys.stdout.flush()
        return None
    if mode == "memory-growth":
        chunks = [b"x" * (1024 * 1024) for _ in range(2)]
        raise MemoryError(f"deterministic test growth reached {len(chunks)} MiB")
    if mode in {"absent-certificate", "corrupt-certificate", "checker-failure"}:
        return WorkerResponse(BENCHMARK_PROTOCOL_V1, request.backend, request.request_id, "UNKNOWN", None, _zero_phase_timings(), (mode,))
    raise WorkerProtocolError(f"unsupported test mode: {mode}")


def _run_worker(args: argparse.Namespace) -> int:
    mode = args.test_mode
    backend = "test" if mode else args.backend
    if backend not in SUPPORTED_BACKENDS or (not mode and backend not in NATIVE_BACKENDS):
        print("unsupported backend", file=sys.stderr)
        return 2
    if args.memory_limit_bytes is not None:
        _apply_rlimit_as(args.memory_limit_bytes)
    handshake_protocol = "unsupported.protocol.v9" if mode == "protocol-mismatch" else BENCHMARK_PROTOCOL_V1
    sys.stdout.buffer.write(
        _encode_line({"backend": backend, "pid": os.getpid(), "protocol": handshake_protocol, "version": WORKER_VERSION})
    )
    sys.stdout.buffer.flush()
    seen: set[str] = set()
    while True:
        raw = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
        if not raw:
            return 0
        try:
            request = decode_request_line(raw)
            if request.request_id in seen:
                raise WorkerProtocolError("duplicate request_id")
            seen.add(request.request_id)
            if request.backend != backend:
                raise WorkerProtocolError("request backend does not match handshake")
            requested_memory_limit = request.limits.memory_limit_bytes
            if args.memory_limit_bytes is not None:
                requested_memory_limit = min(requested_memory_limit, args.memory_limit_bytes)
            _apply_rlimit_as(requested_memory_limit)
            if mode:
                response = _test_response(request, mode)
            else:
                from open_trader.prediction_solver_backends import CpSatBackend, HighsBackend, ScipBackend

                adapter = _TimingBackend({"highs": HighsBackend, "scip": ScipBackend, "cp_sat": CpSatBackend}[request.backend]())
                started_ns = time.perf_counter_ns()
                phase_timings = _zero_phase_timings()

                def observe_phase(name: str) -> None:
                    if name not in {"first_qualified", "optimal"}:
                        raise ValueError(f"unsupported observed phase: {name}")
                    if phase_timings[name] == 0:
                        phase_timings[name] = time.perf_counter_ns() - started_ns

                evidence = solve_with_constraint_generation(
                    request.request,
                    adapter,
                    BenchmarkLimits(
                        request.limits.soft_time_limit_ms,
                        request.limits.hard_time_limit_ms,
                        request.limits.memory_limit_bytes,
                        request.limits.max_constraint_generation_rounds,
                    ),
                    phase_observer=observe_phase,
                )
                phase_timings["backend"] = adapter.solve_ns
                if evidence.certificate is not None:
                    phase_timings["certificate_generation"] = evidence.certificate.generation_ns
                    phase_timings["certificate_completion"] = evidence.certificate.completion_ns
                    phase_timings["certificate_check"] = evidence.certificate.check_ns
                serialization_started_ns = time.perf_counter_ns()
                evidence_payload = canonical_payload(evidence)
                phase_timings["serialization"] = time.perf_counter_ns() - serialization_started_ns
                response = WorkerResponse(BENCHMARK_PROTOCOL_V1, backend, request.request_id, "OK", evidence_payload, phase_timings, ())
            if response is not None:
                sys.stdout.buffer.write(encode_response_line(response))
                sys.stdout.buffer.flush()
        except MemoryError as exc:
            print(f"worker memory limit reached: {exc}", file=sys.stderr, flush=True)
            return 3
        except (WorkerProtocolError, ModelDecodeError, ValueError, RuntimeError) as exc:
            print(f"worker request rejected: {exc}", file=sys.stderr, flush=True)
            return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=sorted(NATIVE_BACKENDS))
    parser.add_argument("--memory-limit-bytes", type=int)
    parser.add_argument("--test-mode", choices=("ok", "cooperative-timeout", "hang-child", "exit17", "malformed-json", "truncated-json", "protocol-mismatch", "memory-growth", "absent-certificate", "corrupt-certificate", "checker-failure"))
    args = parser.parse_args(argv)
    if args.test_mode and args.backend is not None:
        parser.error("--test-mode and --backend are mutually exclusive")
    if not args.test_mode and args.backend is None:
        parser.error("--backend is required")
    return _run_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
