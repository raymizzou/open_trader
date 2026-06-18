from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .dashboard import DashboardConfig, load_dashboard_state
from .dashboard_quotes import DashboardQuoteService


STATIC_DIR = Path(__file__).with_name("dashboard_static")


def build_dashboard_payload(config: DashboardConfig) -> dict[str, Any]:
    return load_dashboard_state(config).to_dict()


def build_quotes_payload(quote_service: DashboardQuoteService) -> dict[str, Any]:
    return quote_service.refresh().to_dict()


def create_dashboard_server(
    config: DashboardConfig,
    host: str,
    port: int,
    quote_service: DashboardQuoteService | None = None,
) -> ThreadingHTTPServer:
    service = quote_service or DashboardQuoteService(config=config)

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                return
            if path == "/static/dashboard.css":
                self._send_file(STATIC_DIR / "dashboard.css", "text/css; charset=utf-8")
                return
            if path == "/static/dashboard.js":
                self._send_file(
                    STATIC_DIR / "dashboard.js",
                    "application/javascript; charset=utf-8",
                )
                return
            if path == "/api/dashboard":
                try:
                    self._send_json(build_dashboard_payload(config))
                except Exception as exc:
                    self._send_error_json(exc)
                return
            if path == "/api/quotes":
                try:
                    self._send_json(build_quotes_payload(service))
                except Exception as exc:
                    self._send_error_json(exc)
                return
            self._send_not_found()

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(
            self,
            payload: dict[str, Any],
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, error: Exception) -> None:
            self._send_json(
                {
                    "status": "error",
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        def _send_file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                self._send_not_found()
                return

            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_not_found(self) -> None:
            body = b"not found"
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), DashboardRequestHandler)


def serve_dashboard(config: DashboardConfig, *, host: str, port: int) -> None:
    server = create_dashboard_server(config=config, host=host, port=port)
    _, actual_port = server.server_address
    try:
        print(f"dashboard_url: http://{host}:{actual_port}")
        print(f"portfolio: {config.portfolio_path}")
        print(f"futu: {config.futu_host}:{config.futu_port}")
        print(f"poll_seconds: {config.poll_seconds}")
        server.serve_forever()
    finally:
        server.server_close()
