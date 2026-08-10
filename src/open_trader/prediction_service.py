from __future__ import annotations

import argparse
from datetime import datetime
import ipaddress
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import subprocess
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .prediction_read_model import prediction_history_payload, prediction_state_payload
from .prediction_runtime import PredictionRuntime


_HISTORY_DEFAULT_LIMIT = 100
_HISTORY_MAX_LIMIT = 500
_READ_ONLY_ERROR = {
    "code": "shadow_read_only",
    "message": "Shadow Prediction Service is read-only",
}


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
    *, runtime: PredictionRuntime, host: str = "127.0.0.1", port: int = 8769
) -> ThreadingHTTPServer:
    _require_loopback_host(host)
    if _shadow_evidence(runtime).get("mode") != "shadow":
        raise ValueError("prediction service requires shadow mode")
    metadata = _runtime_metadata()

    class PredictionRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: HTTPStatus, payload: Mapping[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send_unavailable(self) -> None:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "shadow runtime is unavailable"},
            )

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                evidence = _shadow_evidence(runtime)
                available = _is_available(runtime)
                self._send_json(
                    HTTPStatus.OK if available else HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "schema_version": "open_trader.prediction_service.health.v1",
                        "module": "prediction_service",
                        "status": "running" if available else "unavailable",
                        "mode": "shadow",
                        "production_owner": False,
                        "mutations": "prohibited",
                        "runtime_state": str(getattr(runtime, "state", "")),
                        **metadata,
                        "codex": evidence.get("codex", {}),
                        "first_violation": evidence.get("first_violation"),
                    },
                )
                return
            if parsed.path not in {
                "/api/prediction-arbitrage/state",
                "/api/prediction-arbitrage/history",
            }:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not _is_available(runtime):
                self._send_unavailable()
                return
            if parsed.path == "/api/prediction-arbitrage/state":
                self._send_json(
                    HTTPStatus.OK,
                    prediction_state_payload(
                        store=getattr(runtime, "store", None),
                        monitor=getattr(runtime, "monitor", None),
                        execution=getattr(runtime, "execution", None),
                        csrf_token="",
                        cross_venue_monitor=getattr(runtime, "cross_venue_monitor", None),
                    ),
                )
                return
            try:
                query = parse_qs(parsed.query, keep_blank_values=True)
                self._send_json(
                    HTTPStatus.OK,
                    prediction_history_payload(
                        getattr(runtime, "store", None),
                        kind=str(query.get("kind", [""])[0]).strip(),
                        limit=_query_int(query, "limit", _HISTORY_DEFAULT_LIMIT),
                        offset=_query_int(query, "offset", 0),
                        monitor=getattr(runtime, "monitor", None),
                        execution=getattr(runtime, "execution", None),
                        cross_venue_monitor=getattr(runtime, "cross_venue_monitor", None),
                    ),
                )
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_POST(self) -> None:
            if urlparse(self.path).path.startswith("/api/prediction-arbitrage/"):
                self._send_json(HTTPStatus.FORBIDDEN, _READ_ONLY_ERROR)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    server = ThreadingHTTPServer((host, port), PredictionRequestHandler)
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
) -> int:
    _require_loopback_host(host)
    if mode != "shadow":
        raise ValueError("prediction service only supports shadow mode")
    runtime = PredictionRuntime(
        data_dir=Path(data_dir),
        prediction_config_path=Path(prediction_config_path),
        dashboard_url=f"http://{host}:{port}",
        mode="shadow",
    )
    server: ThreadingHTTPServer | None = None
    previous_handlers: dict[int, Any] = {}
    stopping = False

    def stop_handler(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    try:
        runtime.start()
        server = create_prediction_server(runtime=runtime, host=host, port=port)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, stop_handler)
        while not stopping:
            server.handle_request()
            if runtime.poll_shadow_failure() is not None:
                break
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        try:
            runtime.stop()
        finally:
            if server is not None:
                server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open_trader prediction-service")
    parser.add_argument("--mode", default="shadow")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args(argv)
    return serve_prediction_service(
        data_dir=args.data_dir,
        prediction_config_path=args.config,
        host=args.host,
        port=args.port,
        mode=args.mode,
    )
