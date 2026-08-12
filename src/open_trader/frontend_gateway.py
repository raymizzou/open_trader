from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlsplit


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

_STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/static/dashboard.js": (
        "dashboard.js",
        "application/javascript; charset=utf-8",
    ),
}

_PREDICTION_ROUTE_SCHEMA = "open_trader.frontend_gateway.prediction_route.v1"
_PREDICTION_ROUTE_MODES = {"legacy", "maintenance", "service"}


@dataclass(frozen=True)
class FrontendGatewayConfig:
    static_dir: Path
    prediction_route_path: Path
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 8767
    account_upstream_host: str = "127.0.0.1"
    account_upstream_port: int = 8768
    prediction_upstream_host: str = "127.0.0.1"
    prediction_upstream_port: int = 8769
    public_origin: str = "http://127.0.0.1:8766"
    upstream_timeout_seconds: float = 30.0
    max_request_body_bytes: int = 20 * 1024 * 1024


@dataclass(frozen=True)
class _PredictionRoute:
    mode: str
    operation_id: str
    updated_at: str


def _read_prediction_route(path: Path) -> _PredictionRoute:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {"schema_version", "mode", "operation_id", "updated_at"}
            or payload["schema_version"] != _PREDICTION_ROUTE_SCHEMA
            or payload["mode"] not in _PREDICTION_ROUTE_MODES
            or not isinstance(payload["operation_id"], str)
            or not payload["operation_id"]
            or not isinstance(payload["updated_at"], str)
            or not payload["updated_at"]
        ):
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError):
        return _PredictionRoute("maintenance", "invalid-route-record", "")
    return _PredictionRoute(
        payload["mode"], payload["operation_id"], payload["updated_at"]
    )


class _PredictionRouteController:
    def __init__(self, route_path: Path) -> None:
        self._route_path = route_path
        self._lock = threading.Lock()
        self._inflight_requests = 0

    def begin(self) -> _PredictionRoute:
        with self._lock:
            route = _read_prediction_route(self._route_path)
            self._inflight_requests += 1
            return route

    def end(self) -> None:
        with self._lock:
            self._inflight_requests -= 1

    def snapshot(self) -> tuple[_PredictionRoute, int]:
        with self._lock:
            return _read_prediction_route(self._route_path), self._inflight_requests


def create_frontend_gateway(
    *,
    config: FrontendGatewayConfig,
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    _require_loopback(host, "host")
    _require_loopback(config.upstream_host, "upstream_host")
    _require_loopback(config.account_upstream_host, "account_upstream_host")
    _require_loopback(config.prediction_upstream_host, "prediction_upstream_host")
    if not (
        0 <= port <= 65535
        and 1 <= config.upstream_port <= 65535
        and 1 <= config.account_upstream_port <= 65535
        and 1 <= config.prediction_upstream_port <= 65535
    ):
        raise ValueError("ports must be between 1 and 65535")
    if config.upstream_timeout_seconds <= 0:
        raise ValueError("upstream timeout must be positive")
    if config.max_request_body_bytes < 0:
        raise ValueError("request body limit must not be negative")

    public_origin = _normalized_origin(config.public_origin)
    upstream_authority = _host_port(config.upstream_host, config.upstream_port)
    upstream_origin = f"http://{upstream_authority}"
    account_upstream_authority = _host_port(
        config.account_upstream_host, config.account_upstream_port
    )
    account_upstream_origin = f"http://{account_upstream_authority}"
    prediction_upstream_authority = _host_port(
        config.prediction_upstream_host, config.prediction_upstream_port
    )
    prediction_upstream_origin = f"http://{prediction_upstream_authority}"
    prediction_controller = _PredictionRouteController(config.prediction_route_path)
    runtime = _runtime_metadata()

    class FrontendGatewayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in _STATIC_ROUTES:
                filename, content_type = _STATIC_ROUTES[path]
                self._send_file(config.static_dir / filename, content_type)
                return
            if path == "/healthz":
                legacy_upstream_status = self._legacy_upstream_status()
                prediction_route, prediction_inflight_requests = (
                    prediction_controller.snapshot()
                )
                self._send_json(
                    {
                        "schema_version": "open_trader.frontend_gateway.health.v1",
                        "module": "frontend_gateway",
                        **runtime,
                        "upstream_status": legacy_upstream_status,
                        "legacy_upstream_status": legacy_upstream_status,
                        "account_upstream_status": self._account_upstream_status(),
                        "prediction_route_mode": prediction_route.mode,
                        "prediction_inflight_requests": prediction_inflight_requests,
                        "prediction_upstream_status": self._prediction_upstream_status(
                            prediction_route
                        ),
                    }
                )
                return
            if _is_api_path(path):
                self._proxy()
                return
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Not found")

        def do_POST(self) -> None:
            if _is_api_path(urlsplit(self.path).path):
                self._proxy()
                return
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Not found")

        def _proxy(self) -> None:
            path = urlsplit(self.path).path
            prediction_route = (
                prediction_controller.begin() if _is_prediction_path(path) else None
            )
            if prediction_route is not None and prediction_route.mode == "maintenance":
                prediction_controller.end()
                self._send_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "prediction_maintenance",
                    "Prediction service is in maintenance",
                    route_mode="maintenance",
                )
                return

            account_route = _is_account_path(path)
            (
                target_host,
                target_port,
                target_authority,
                target_origin,
                unavailable_code,
                unavailable_message,
            ) = (
                (
                    config.prediction_upstream_host,
                    config.prediction_upstream_port,
                    prediction_upstream_authority,
                    prediction_upstream_origin,
                    "prediction_service_unavailable",
                    "Prediction service is unavailable",
                )
                if prediction_route is not None and prediction_route.mode == "service"
                else
                (
                    config.account_upstream_host,
                    config.account_upstream_port,
                    account_upstream_authority,
                    account_upstream_origin,
                    "account_module_unavailable",
                    "Account module is unavailable",
                )
                if account_route
                else (
                    config.upstream_host,
                    config.upstream_port,
                    upstream_authority,
                    upstream_origin,
                    "legacy_dashboard_unavailable",
                    "Legacy Dashboard is unavailable",
                )
            )
            origin = self.headers.get("Origin", "")
            try:
                if self.command == "POST" and origin and origin != public_origin:
                    self._send_error(
                        HTTPStatus.FORBIDDEN,
                        "untrusted_origin",
                        "Origin is not trusted",
                    )
                    return
                try:
                    body = self._request_body()
                except ValueError as error:
                    status = (
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                        if str(error) == "request body is too large"
                        else HTTPStatus.BAD_REQUEST
                    )
                    self._send_error(status, "invalid_request_body", str(error))
                    return

                request_headers = self._upstream_headers(
                    body,
                    origin,
                    authority=target_authority,
                    target_origin=target_origin,
                    account_route=account_route,
                )
                connection = http.client.HTTPConnection(
                    target_host,
                    target_port,
                    timeout=config.upstream_timeout_seconds,
                )
                try:
                    connection.request(
                        self.command,
                        self.path,
                        body=body if self.command == "POST" else None,
                        headers=request_headers,
                    )
                    response = connection.getresponse()
                    response_body = response.read()
                    response_headers = response.getheaders()
                    status = response.status
                    reason = response.reason
                except (OSError, http.client.HTTPException, TimeoutError):
                    self._send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        unavailable_code,
                        unavailable_message,
                    )
                    return
                finally:
                    connection.close()

                excluded = _hop_by_hop_names(response_headers) | {"content-length"}
                self.send_response_only(status, reason)
                for name, value in response_headers:
                    if name.lower() not in excluded:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self._write(response_body)
            finally:
                if prediction_route is not None:
                    prediction_controller.end()

        def _request_body(self) -> bytes:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Transfer-Encoding is not supported")
            raw_lengths = self.headers.get_all("Content-Length", [])
            if self.command == "POST" and len(raw_lengths) != 1:
                raise ValueError("Content-Length is required")
            if not raw_lengths:
                return b""
            raw_length = raw_lengths[0]
            if not raw_length.isdigit():
                raise ValueError("Content-Length must be a non-negative integer")
            content_length = int(raw_length)
            if content_length > config.max_request_body_bytes:
                raise ValueError("request body is too large")
            body = self.rfile.read(content_length)
            if len(body) != content_length:
                raise ValueError("request body is incomplete")
            return body

        def _upstream_headers(
            self,
            body: bytes,
            origin: str,
            *,
            authority: str,
            target_origin: str,
            account_route: bool,
        ) -> dict[str, str]:
            excluded = _hop_by_hop_names(self.headers.items()) | {
                "content-length",
                "host",
                "x-open-trader-account-route",
            }
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in excluded
            }
            headers["Host"] = authority
            if origin == public_origin:
                headers["Origin"] = target_origin
            referer = self.headers.get("Referer", "")
            if referer and _url_has_origin(referer, public_origin):
                headers["Referer"] = target_origin + referer[len(public_origin) :]
            if self.command == "POST":
                headers["Content-Length"] = str(len(body))
            if account_route:
                headers["X-Open-Trader-Account-Route"] = "production"
            return headers

        def _legacy_upstream_status(self) -> str:
            connection = http.client.HTTPConnection(
                config.upstream_host,
                config.upstream_port,
                timeout=config.upstream_timeout_seconds,
            )
            try:
                connection.request("GET", "/healthz", headers={"Host": upstream_authority})
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                if response.status == HTTPStatus.OK and payload.get("module") == "legacy_dashboard":
                    return "ok"
            except (OSError, ValueError, http.client.HTTPException, TimeoutError):
                pass
            finally:
                connection.close()
            return "unavailable"

        def _account_upstream_status(self) -> str:
            connection = http.client.HTTPConnection(
                config.account_upstream_host,
                config.account_upstream_port,
                timeout=config.upstream_timeout_seconds,
            )
            try:
                connection.request(
                    "GET", "/healthz", headers={"Host": account_upstream_authority}
                )
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                if (
                    response.status == HTTPStatus.OK
                    and payload.get("module") == "account_api"
                    and payload.get("mode") == "production"
                ):
                    return "ok"
            except (OSError, ValueError, http.client.HTTPException, TimeoutError):
                pass
            finally:
                connection.close()
            return "unavailable"

        def _prediction_upstream_status(self, route: _PredictionRoute) -> str:
            if route.mode != "service":
                return "not_selected"
            connection = http.client.HTTPConnection(
                config.prediction_upstream_host,
                config.prediction_upstream_port,
                timeout=config.upstream_timeout_seconds,
            )
            try:
                connection.request(
                    "GET", "/healthz", headers={"Host": prediction_upstream_authority}
                )
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                if (
                    response.status == HTTPStatus.OK
                    and isinstance(payload, dict)
                    and payload.get("module") == "prediction_service"
                ):
                    return "ok"
            except (OSError, ValueError, http.client.HTTPException, TimeoutError):
                pass
            finally:
                connection.close()
            return "unavailable"

        def _send_file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Not found")
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._write(body)

        def _send_json(
            self,
            payload: dict[str, object],
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._write(body)

        def _send_error(
            self,
            status: HTTPStatus,
            code: str,
            message: str,
            *,
            route_mode: str | None = None,
        ) -> None:
            payload: dict[str, object] = {
                "schema_version": "open_trader.frontend_gateway.error.v1",
                "code": code,
                "message": message,
            }
            if route_mode is not None:
                payload["route_mode"] = route_mode
            self._send_json(
                payload,
                status,
            )

        def _write(self, body: bytes) -> None:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), FrontendGatewayHandler)
    server.daemon_threads = True
    server.runtime_metadata = runtime  # type: ignore[attr-defined]
    return server


def serve_frontend_gateway(
    *,
    config: FrontendGatewayConfig,
    host: str,
    port: int,
) -> None:
    server = create_frontend_gateway(config=config, host=host, port=port)
    runtime = {
        "schema_version": "open_trader.frontend_gateway.runtime.v1",
        "module": "frontend_gateway",
        **server.runtime_metadata,  # type: ignore[attr-defined]
        "host": host,
        "port": server.server_address[1],
        "upstream": _host_port(config.upstream_host, config.upstream_port),
        "account_upstream": _host_port(
            config.account_upstream_host, config.account_upstream_port
        ),
        "prediction_upstream": _host_port(
            config.prediction_upstream_host, config.prediction_upstream_port
        ),
    }
    print(f"frontend_gateway_runtime: {json.dumps(runtime)}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open-trader frontend-gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=8767)
    parser.add_argument("--account-upstream-host", default="127.0.0.1")
    parser.add_argument("--account-upstream-port", type=int, default=8768)
    parser.add_argument("--prediction-route-state", type=Path, required=True)
    parser.add_argument("--prediction-upstream-host", default="127.0.0.1")
    parser.add_argument("--prediction-upstream-port", type=int, default=8769)
    parser.add_argument("--public-origin", default="http://127.0.0.1:8766")
    parser.add_argument("--upstream-timeout", type=float, default=30.0)
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=Path(__file__).with_name("dashboard_static"),
    )
    args = parser.parse_args(argv)
    serve_frontend_gateway(
        config=FrontendGatewayConfig(
            static_dir=args.static_dir,
            upstream_host=args.upstream_host,
            upstream_port=args.upstream_port,
            account_upstream_host=args.account_upstream_host,
            account_upstream_port=args.account_upstream_port,
            prediction_route_path=args.prediction_route_state,
            prediction_upstream_host=args.prediction_upstream_host,
            prediction_upstream_port=args.prediction_upstream_port,
            public_origin=args.public_origin,
            upstream_timeout_seconds=args.upstream_timeout,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _is_account_path(path: str) -> bool:
    if path == "/api/v1/account/snapshot":
        return True
    return path.startswith("/api/v1/account/statements/")


def _is_prediction_path(path: str) -> bool:
    return path == "/api/prediction-arbitrage" or path.startswith(
        "/api/prediction-arbitrage/"
    )


def _hop_by_hop_names(headers: object) -> set[str]:
    pairs = list(headers)  # type: ignore[arg-type]
    names = set(_HOP_BY_HOP_HEADERS)
    for name, value in pairs:
        if name.lower() == "connection":
            names.update(token.strip().lower() for token in value.split(",") if token.strip())
    return names


def _normalized_origin(value: str) -> str:
    origin = value.rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path:
        raise ValueError("public_origin must be an HTTP origin without a path")
    _require_loopback(parsed.hostname, "public_origin")
    return origin


def _url_has_origin(url: str, origin: str) -> bool:
    parsed_url = urlsplit(url)
    parsed_origin = urlsplit(origin)
    return (
        parsed_url.scheme == parsed_origin.scheme
        and parsed_url.netloc == parsed_origin.netloc
    )


def _require_loopback(host: str, field: str) -> None:
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError(f"{field} must be a loopback address") from error
    if not address.is_loopback:
        raise ValueError(f"{field} must be a loopback address")


def _host_port(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _runtime_metadata() -> dict[str, object]:
    cwd = Path.cwd().resolve()
    try:
        git_sha = subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"], text=True
        ).strip()
        source_status = subprocess.check_output(
            [
                "git",
                "-C",
                str(cwd),
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                "src/open_trader",
            ],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        git_sha = ""
        source_status = "unavailable"
    return {
        "pid": os.getpid(),
        "started_at": datetime.now().astimezone().isoformat(),
        "cwd": str(cwd),
        "git_sha": git_sha,
        "source_state": "clean" if not source_status else "dirty",
    }
