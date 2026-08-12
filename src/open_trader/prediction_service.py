from __future__ import annotations

import argparse
from concurrent.futures import Future
from http.cookies import SimpleCookie
from datetime import datetime
import ipaddress
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from .prediction_read_model import (
    PREDICTION_HISTORY_KINDS,
    _prediction_safe_value,
    prediction_history_payload,
    prediction_state_payload,
)
from .prediction_release import load_prediction_release_manifest
from .prediction_runtime import PredictionRuntime


_HISTORY_DEFAULT_LIMIT = 100
_HISTORY_MAX_LIMIT = 500
_MAX_JSON_BODY_BYTES = 1024 * 1024
_MAX_CONCURRENT_HTTP_REQUESTS = 8
_HISTORY_CACHE_SECONDS = 1.0
_HISTORY_WAIT_SECONDS = 5.0
_BUSY_BODY = b'{"error":"prediction service busy"}'
_READ_ONLY_ERROR = {
    "code": "shadow_read_only",
    "message": "Shadow Prediction Service is read-only",
}


class _PredictionHTTPServer(ThreadingHTTPServer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_HTTP_REQUESTS)
        self._http_load_lock = threading.Lock()
        self._http_active = 0
        self._overload_rejections = 0
        self._history_cache_hits = 0
        self._history_cache_misses = 0
        self._history_cache: dict[
            tuple[str, int, int], tuple[float, dict[str, object]]
        ] = {}
        self._history_flights: dict[
            tuple[str, int, int], Future[dict[str, object]]
        ] = {}
        self._busy_response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Content-Length: "
            + str(len(_BUSY_BODY)).encode("ascii")
            + b"\r\nRetry-After: 1\r\nConnection: close\r\n\r\n"
            + _BUSY_BODY
        )

    def process_request(self, request: object, client_address: object) -> None:
        if not self._request_slots.acquire(blocking=False):
            self._record_overload()
            try:
                request.sendall(self._busy_response)  # type: ignore[attr-defined]
            except OSError:
                pass
            finally:
                self.shutdown_request(request)  # type: ignore[arg-type]
            return
        self._record_admitted()
        try:
            super().process_request(request, client_address)  # type: ignore[arg-type]
        except BaseException:
            self._release_admitted()
            raise

    def process_request_thread(self, request: object, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)  # type: ignore[arg-type]
        finally:
            self._release_admitted()

    def http_load_snapshot(self) -> dict[str, int]:
        with self._http_load_lock:
            return {
                "limit": _MAX_CONCURRENT_HTTP_REQUESTS,
                "active": self._http_active,
                "overload_rejections": self._overload_rejections,
                "history_cache_hits": self._history_cache_hits,
                "history_cache_misses": self._history_cache_misses,
            }

    def history_payload(
        self,
        key: tuple[str, int, int],
        compute: Callable[[], dict[str, object]],
    ) -> dict[str, object]:
        with self._http_load_lock:
            now = time.monotonic()
            for cache_key, (expires_at, _) in tuple(self._history_cache.items()):
                if expires_at <= now:
                    del self._history_cache[cache_key]
            cached = self._history_cache.get(key)
            if cached is not None:
                self._history_cache_hits += 1
                return cached[1]
            flight = self._history_flights.get(key)
            if flight is None:
                flight = Future()
                self._history_flights[key] = flight
                self._history_cache_misses += 1
                leader = True
            else:
                self._history_cache_hits += 1
                leader = False

        if not leader:
            return flight.result(timeout=_HISTORY_WAIT_SECONDS)
        try:
            payload = compute()
        except BaseException as exc:
            with self._http_load_lock:
                if self._history_flights.get(key) is flight:
                    del self._history_flights[key]
                    flight.set_exception(exc)
            raise
        with self._http_load_lock:
            self._history_cache[key] = (time.monotonic() + _HISTORY_CACHE_SECONDS, payload)
            if self._history_flights.get(key) is flight:
                del self._history_flights[key]
                flight.set_result(payload)
        return payload

    def _record_admitted(self) -> None:
        with self._http_load_lock:
            self._http_active += 1

    def _release_admitted(self) -> None:
        with self._http_load_lock:
            self._http_active -= 1
        self._request_slots.release()

    def _record_overload(self) -> None:
        with self._http_load_lock:
            self._overload_rejections += 1


def _require_loopback_host(host: str) -> None:
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError("prediction service requires a loopback host")


def _runtime_metadata() -> dict[str, object]:
    cwd = Path.cwd().resolve()
    try:
        git_sha = subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        git_sha = ""
    return {
        "pid": os.getpid(),
        "cwd": str(cwd),
        "git_sha": git_sha,
        "started_at": datetime.now().astimezone().isoformat(),
    }


def _shadow_evidence(runtime: PredictionRuntime) -> Mapping[str, object]:
    evidence = getattr(runtime, "shadow_evidence", {})
    return evidence if isinstance(evidence, Mapping) else {}


def _is_available(runtime: PredictionRuntime) -> bool:
    evidence = _shadow_evidence(runtime)
    return (
        getattr(runtime, "state", None) == "RUNNING"
        and evidence.get("mode") == "shadow"
        and evidence.get("first_violation") is None
    )


def _is_production_available(runtime: PredictionRuntime) -> bool:
    return (
        getattr(runtime, "state", None) == "RUNNING"
        and getattr(runtime, "production_owner", False) is True
    )


def _query_int(query: Mapping[str, list[str]], key: str, default: int) -> int:
    raw = str(query.get(key, [str(default)])[0] or str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a non-negative integer") from exc
    if value < 0 or (key == "limit" and value > _HISTORY_MAX_LIMIT):
        raise ValueError(f"{key} is outside the allowed range")
    if key == "limit" and value == 0:
        raise ValueError("limit must be positive")
    return value


def create_prediction_server(
    *,
    runtime: PredictionRuntime,
    host: str = "127.0.0.1",
    port: int = 8769,
    session_token: str | None = None,
    csrf_token: str | None = None,
    runtime_metadata: Mapping[str, object] | None = None,
) -> ThreadingHTTPServer:
    _require_loopback_host(host)
    mode = str(getattr(runtime, "mode", _shadow_evidence(runtime).get("mode", "")))
    if mode not in {"shadow", "production"}:
        raise ValueError("prediction service mode is invalid")
    if mode == "production" and not _is_production_available(runtime):
        raise RuntimeError("production runtime is not ready")
    prediction_session = session_token or secrets.token_urlsafe(32)
    prediction_csrf = csrf_token or secrets.token_urlsafe(32)
    metadata = dict(runtime_metadata if runtime_metadata is not None else _runtime_metadata())

    class PredictionRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(
            self,
            status: HTTPStatus,
            payload: Mapping[str, object],
            *,
            set_session: bool = False,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if set_session:
                self.send_header(
                    "Set-Cookie",
                    f"ot_prediction_session={prediction_session}; SameSite=Strict; HttpOnly; Path=/",
                )
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send_unavailable(self) -> None:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": f"{mode} runtime is unavailable"},
            )

        def _send_error(self, status: HTTPStatus, error: Exception) -> None:
            self._send_json(
                status,
                {
                    "status": "error",
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
            )

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                evidence = _shadow_evidence(runtime)
                available = (
                    _is_available(runtime)
                    if mode == "shadow"
                    else _is_production_available(runtime)
                )
                self._send_json(
                    HTTPStatus.OK if available else HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "schema_version": "open_trader.prediction_service.health.v1",
                        "module": "prediction_service",
                        "status": "running" if available else "unavailable",
                        "mode": mode,
                        "production_owner": (
                            getattr(runtime, "production_owner", False) is True
                        ),
                        "mutations": "prohibited" if mode == "shadow" else "enabled",
                        "runtime_state": str(getattr(runtime, "state", "")),
                        **metadata,
                        "codex": evidence.get("codex", {}),
                        "first_violation": evidence.get("first_violation"),
                        "guard_attempts": evidence.get("guard_attempts", []),
                        "http_load": self.server.http_load_snapshot(),  # type: ignore[attr-defined]
                    },
                )
                return
            if parsed.path not in {
                "/api/prediction-arbitrage/state",
                "/api/prediction-arbitrage/history",
            }:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if (
                mode == "shadow"
                and not _is_available(runtime)
                or mode == "production"
                and not _is_production_available(runtime)
            ):
                self._send_unavailable()
                return
            if parsed.path == "/api/prediction-arbitrage/state":
                self._send_json(
                    HTTPStatus.OK,
                    prediction_state_payload(
                        store=getattr(runtime, "store", None),
                        monitor=getattr(runtime, "monitor", None),
                        execution=getattr(runtime, "execution", None),
                        csrf_token="" if mode == "shadow" else prediction_csrf,
                        cross_venue_monitor=getattr(runtime, "cross_venue_monitor", None),
                    ),
                    set_session=mode == "production",
                )
                return
            try:
                query = parse_qs(parsed.query, keep_blank_values=True)
                kind = str(query.get("kind", [""])[0]).strip()
                limit = _query_int(query, "limit", _HISTORY_DEFAULT_LIMIT)
                offset = _query_int(query, "offset", 0)
                if kind not in PREDICTION_HISTORY_KINDS:
                    raise ValueError("kind must be signals, executions, or incidents")
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            try:
                self._send_json(
                    HTTPStatus.OK,
                    self.server.history_payload(  # type: ignore[attr-defined]
                        (kind, limit, offset),
                        lambda: prediction_history_payload(
                            getattr(runtime, "store", None),
                            kind=kind,
                            limit=limit,
                            offset=offset,
                            monitor=getattr(runtime, "monitor", None),
                            execution=getattr(runtime, "execution", None),
                            cross_venue_monitor=getattr(runtime, "cross_venue_monitor", None),
                        ),
                    ),
                )
            except Exception:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "prediction history unavailable"},
                )

        def do_POST(self) -> None:
            self.close_connection = True
            path = urlparse(self.path).path
            if mode == "shadow" and path.startswith("/api/prediction-arbitrage/"):
                self._send_json(HTTPStatus.FORBIDDEN, _READ_ONLY_ERROR)
                return
            if mode == "production" and path.startswith(
                "/api/prediction-arbitrage/"
            ):
                try:
                    self._require_production_auth()
                except PermissionError as exc:
                    self._send_error(HTTPStatus.FORBIDDEN, exc)
                    return
                if not _is_production_available(runtime):
                    self._send_unavailable()
                    return
            execution_mutation = path in {
                "/api/prediction-arbitrage/preview",
                "/api/prediction-arbitrage/executions",
            }
            if path not in {
                "/api/prediction-arbitrage/preview",
                "/api/prediction-arbitrage/executions",
                "/api/prediction-arbitrage/mode",
                "/api/prediction-arbitrage/circuit-breaker/reset",
                "/api/prediction-arbitrage/predict-allowance/cleanup",
                "/api/prediction-arbitrage/cross-auto/pause",
            }:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                payload = self._read_json_body()
                execution = getattr(runtime, "execution", None)
                if execution is None:
                    raise RuntimeError("prediction execution service is unavailable")
                audit = (
                    {}
                    if execution_mutation
                    else self._audit_context()
                )
                if path.endswith("/preview"):
                    self._require_schema(payload, {"opportunity_id"})
                    result = execution.preview(
                        self._required_string(payload, "opportunity_id")
                    )
                elif path.endswith("/executions"):
                    self._require_schema(payload, {"preview_id", "idempotency_key"})
                    result = execution.confirm(
                        self._required_string(payload, "preview_id"),
                        self._required_string(payload, "idempotency_key"),
                    )
                elif path.endswith("/mode"):
                    self._require_schema(payload, {"mode"})
                    result = execution.set_validation_mode(
                        self._required_string(payload, "mode"), audit=audit
                    )
                elif path.endswith("/circuit-breaker/reset"):
                    self._require_schema(payload, {"incident_id"})
                    result = execution.reset_breaker(
                        self._required_string(payload, "incident_id"), audit=audit
                    )
                elif path.endswith("/cross-auto/pause"):
                    self._require_confirm(payload)
                    result = execution.pause_cross_auto(audit=audit)
                else:
                    self._require_confirm(payload)
                    result = execution.cleanup_predict_allowance(
                        confirm=True, audit=audit
                    )
                status = HTTPStatus.OK
                if (
                    not execution_mutation
                    and isinstance(result, Mapping)
                    and result.get("state") == "busy"
                ):
                    status = HTTPStatus.CONFLICT
                safe_result = _prediction_safe_value(result)
                if not isinstance(safe_result, Mapping):
                    raise RuntimeError("prediction mutation result is invalid")
                self._send_json(status, safe_result)
            except PermissionError as exc:
                self._send_error(HTTPStatus.FORBIDDEN, exc)
            except OverflowError as exc:
                self._send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)}
                )
            except ValueError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, exc)
            except (sqlite3.Error, OSError, RuntimeError) as exc:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})

        def _listener_host_header(self) -> str:
            bound_host = str(host)
            if ":" in bound_host and not bound_host.startswith("["):
                bound_host = f"[{bound_host}]"
            return f"{bound_host}:{int(self.server.server_address[1])}"

        def _require_production_auth(self) -> None:
            try:
                if not ipaddress.ip_address(str(self.client_address[0])).is_loopback:
                    raise PermissionError("prediction mutations require loopback")
            except ValueError as exc:
                raise PermissionError("prediction mutations require loopback") from exc
            expected_host = self._listener_host_header()
            if self.headers.get("Host", "") != expected_host:
                raise PermissionError("prediction mutation Host is invalid")
            if self.headers.get("Origin", "") != f"http://{expected_host}":
                raise PermissionError("prediction mutation Origin is invalid")
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception as exc:
                raise PermissionError("prediction session is invalid") from exc
            provided_session = cookie.get("ot_prediction_session")
            if provided_session is None or not secrets.compare_digest(
                provided_session.value, prediction_session
            ):
                raise PermissionError("prediction session is invalid")
            if not secrets.compare_digest(
                self.headers.get("X-CSRF-Token", ""), prediction_csrf
            ):
                raise PermissionError("prediction CSRF token is invalid")

        def _read_json_body(self) -> dict[str, object]:
            raw_length = self.headers.get("Content-Length") or "0"
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise ValueError("Content-Length must be a non-negative integer") from exc
            if content_length < 0:
                raise ValueError("Content-Length must be a non-negative integer")
            if content_length > _MAX_JSON_BODY_BYTES:
                raise OverflowError("request body cannot exceed 1 MiB")
            body = self.rfile.read(content_length) if content_length else b"{}"
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("request body must be a JSON object") from exc
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        @staticmethod
        def _require_schema(payload: Mapping[str, object], expected: set[str]) -> None:
            if set(payload) != expected:
                raise ValueError("prediction request fields are invalid")

        @staticmethod
        def _required_string(payload: Mapping[str, object], key: str) -> str:
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} is required")
            return value.strip()

        @classmethod
        def _require_confirm(cls, payload: Mapping[str, object]) -> None:
            cls._require_schema(payload, {"confirm"})
            if payload.get("confirm") is not True:
                raise ValueError("confirm must be true")

        def _audit_context(self) -> dict[str, object]:
            audit = {
                "actor": "local_operator",
                "git_sha": str(metadata.get("git_sha", "")),
            }
            store = getattr(runtime, "store", None)
            policy = getattr(store, "safety_policy", None)
            if callable(policy):
                current = policy()
                if isinstance(current, Mapping):
                    audit["safety_fingerprint"] = str(current.get("fingerprint", ""))
            return audit

        def _unsupported_method(self) -> None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        do_HEAD = _unsupported_method
        do_PUT = _unsupported_method
        do_DELETE = _unsupported_method
        do_OPTIONS = _unsupported_method
        do_PATCH = _unsupported_method
        do_CONNECT = _unsupported_method
        do_TRACE = _unsupported_method

    server = _PredictionHTTPServer((host, port), PredictionRequestHandler)
    server.timeout = 0.2
    return server


create_prediction_service = create_prediction_server


def serve_prediction_service(
    *,
    data_dir: Path,
    prediction_config_path: Path,
    host: str = "127.0.0.1",
    port: int = 8769,
    mode: str = "shadow",
    release_manifest_path: Path | None = None,
) -> int:
    _require_loopback_host(host)
    if mode not in {"shadow", "production"}:
        raise ValueError("prediction service mode is invalid")
    release = None
    if mode == "production":
        if release_manifest_path is None:
            raise ValueError("production release manifest is required")
        release = load_prediction_release_manifest(release_manifest_path)
    metadata = _runtime_metadata()
    if release is not None:
        metadata.update(
            {
                "release_schema_version": release.schema_version,
                "reader_generation": release.reader_generation,
                "contract_generation": release.contract_generation,
            }
        )
    runtime = PredictionRuntime(
        data_dir=Path(data_dir),
        prediction_config_path=Path(prediction_config_path),
        dashboard_url=f"http://{host}:{port}",
        mode=mode,
        git_sha=str(metadata.get("git_sha", "")),
        reader_generation=None if release is None else release.reader_generation,
    )
    server: ThreadingHTTPServer | None = None
    previous_handlers: dict[int, Any] = {}
    stopping = False

    def stop_handler(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, stop_handler)
        runtime.start()
        if mode == "production" and not (
            runtime.state == "RUNNING" and runtime.production_owner is True
        ):
            raise RuntimeError("production runtime is not ready")
        server = create_prediction_server(
            runtime=runtime,
            host=host,
            port=port,
            runtime_metadata=metadata,
        )
        while not stopping:
            server.handle_request()
            if mode == "shadow":
                runtime.poll_shadow_failure()
        return 0
    finally:
        try:
            try:
                runtime.stop()
            finally:
                if server is not None:
                    server.server_close()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open_trader prediction-service")
    parser.add_argument("--mode", default="shadow")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--release-manifest", type=Path)
    args = parser.parse_args(argv)
    return serve_prediction_service(
        data_dir=args.data_dir,
        prediction_config_path=args.config,
        host=args.host,
        port=args.port,
        mode=args.mode,
        release_manifest_path=args.release_manifest,
    )
