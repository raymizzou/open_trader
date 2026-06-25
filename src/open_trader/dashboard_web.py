from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .dashboard import DashboardConfig, load_dashboard_state
from .dashboard_account_sync import DashboardAccountSyncService
from .dashboard_quotes import DashboardQuoteService
from .research_chat import ResearchChatError, ResearchChatService


STATIC_DIR = Path(__file__).with_name("dashboard_static")


def build_dashboard_payload(config: DashboardConfig) -> dict[str, Any]:
    return load_dashboard_state(config).to_dict()


def build_quotes_payload(
    quote_service: DashboardQuoteService,
    account_sync_service: DashboardAccountSyncService | None = None,
) -> dict[str, Any]:
    account_sync_payload = (
        account_sync_service.refresh_if_due().to_dict()
        if account_sync_service is not None
        else {}
    )
    payload = quote_service.refresh().to_dict()
    if account_sync_payload:
        payload["account_sync"] = account_sync_payload
    return payload


def create_dashboard_server(
    config: DashboardConfig,
    host: str,
    port: int,
    quote_service: DashboardQuoteService | None = None,
    account_sync_service: DashboardAccountSyncService | None = None,
    research_chat_service: ResearchChatService | None = None,
) -> ThreadingHTTPServer:
    service = quote_service or DashboardQuoteService(config=config)
    chat_service = research_chat_service or ResearchChatService(data_dir=config.data_dir)

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
                    self._send_json(
                        build_quotes_payload(
                            service,
                            account_sync_service=account_sync_service,
                        )
                    )
                except Exception as exc:
                    self._send_error_json(exc)
                return
            session_id = self._research_chat_session_id(path)
            if session_id is not None:
                try:
                    self._send_json(chat_service.get_session(session_id))
                except Exception as exc:
                    self._send_error_json(exc)
                return
            self._send_not_found()

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                if path == "/api/research-chat/sessions":
                    payload = self._read_json_body()
                    market = str(payload.get("market") or "")
                    symbol = str(payload.get("symbol") or "")
                    if not market or not symbol:
                        raise ResearchChatError("market and symbol are required")
                    self._send_json(
                        chat_service.create_session(
                            market=market,
                            symbol=symbol,
                        )
                    )
                    return
                if path.startswith("/api/research-chat/sessions/"):
                    route = self._research_chat_session_action(path)
                    if route is None:
                        self._send_not_found()
                        return
                    session_id, action = route
                    if action == "messages":
                        payload = self._read_json_body()
                        self._send_json(
                            chat_service.append_message(
                                session_id=session_id,
                                content=str(payload.get("content") or ""),
                            )
                        )
                        return
                    if action == "finalize":
                        self._read_json_body()
                        self._send_json(
                            chat_service.finalize_session(session_id=session_id)
                        )
                        return
            except Exception as exc:
                self._send_error_json(exc)
                return
            self._send_not_found()

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json_body(self) -> dict[str, Any]:
            content_length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(content_length) if content_length else b"{}"
            payload = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ResearchChatError("request body must be a JSON object")
            return payload

        def _research_chat_session_id(self, path: str) -> str | None:
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:3] == ["api", "research-chat", "sessions"]:
                return parts[3]
            return None

        def _research_chat_session_action(self, path: str) -> tuple[str, str] | None:
            parts = path.strip("/").split("/")
            if (
                len(parts) == 5
                and parts[:3] == ["api", "research-chat", "sessions"]
                and parts[3]
                and parts[4] in {"messages", "finalize"}
            ):
                return parts[3], parts[4]
            return None

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
    account_sync_service = DashboardAccountSyncService(config=config)
    server = create_dashboard_server(
        config=config,
        host=host,
        port=port,
        account_sync_service=account_sync_service,
    )
    _, actual_port = server.server_address
    try:
        print(f"dashboard_url: http://{host}:{actual_port}")
        print(f"portfolio: {config.portfolio_path}")
        print(f"futu: {config.futu_host}:{config.futu_port}")
        print(f"poll_seconds: {config.poll_seconds}")
        print(f"account_sync_seconds: {account_sync_service.interval_seconds}")
        server.serve_forever()
    finally:
        server.server_close()
