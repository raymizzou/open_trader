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


@dataclass(frozen=True)
class FrontendGatewayConfig:
    static_dir: Path
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 8767
    public_origin: str = "http://127.0.0.1:8766"
    upstream_timeout_seconds: float = 30.0
    max_request_body_bytes: int = 20 * 1024 * 1024


def create_frontend_gateway(
    *,
    config: FrontendGatewayConfig,
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    _require_loopback(host, "host")
    _require_loopback(config.upstream_host, "upstream_host")
    if not 0 <= port <= 65535 or not 1 <= config.upstream_port <= 65535:
        raise ValueError("ports must be between 1 and 65535")
    if config.upstream_timeout_seconds <= 0:
        raise ValueError("upstream timeout must be positive")
    if config.max_request_body_bytes < 0:
        raise ValueError("request body limit must not be negative")

    public_origin = _normalized_origin(config.public_origin)
    upstream_authority = _host_port(config.upstream_host, config.upstream_port)
    upstream_origin = f"http://{upstream_authority}"
    runtime = _runtime_metadata()

    class FrontendGatewayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in _STATIC_ROUTES:
                filename, content_type = _STATIC_ROUTES[path]
                self._send_file(config.static_dir / filename, content_type)
                return
            if path == "/healthz":
                self._send_json(
                    {
                        "schema_version": "open_trader.frontend_gateway.health.v1",
                        "module": "frontend_gateway",
                        **runtime,
                        "upstream_status": self._upstream_status(),
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
            origin = self.headers.get("Origin", "")
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

            request_headers = self._upstream_headers(body, origin)
            connection = http.client.HTTPConnection(
                config.upstream_host,
                config.upstream_port,
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
                    "legacy_dashboard_unavailable",
                    "Legacy Dashboard is unavailable",
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

        def _upstream_headers(self, body: bytes, origin: str) -> dict[str, str]:
            excluded = _hop_by_hop_names(self.headers.items()) | {
                "content-length",
                "host",
            }
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in excluded
            }
            headers["Host"] = upstream_authority
            if origin == public_origin:
                headers["Origin"] = upstream_origin
            referer = self.headers.get("Referer", "")
            if referer and _url_has_origin(referer, public_origin):
                headers["Referer"] = upstream_origin + referer[len(public_origin) :]
            if self.command == "POST":
                headers["Content-Length"] = str(len(body))
            return headers

        def _upstream_status(self) -> str:
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

        def _send_error(self, status: HTTPStatus, code: str, message: str) -> None:
            self._send_json(
                {
                    "schema_version": "open_trader.frontend_gateway.error.v1",
                    "code": code,
                    "message": message,
                },
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
            public_origin=args.public_origin,
            upstream_timeout_seconds=args.upstream_timeout,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


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
