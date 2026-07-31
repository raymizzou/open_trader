from __future__ import annotations

from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time
from typing import Iterator
import urllib.error
import urllib.request

import pytest

from open_trader.frontend_gateway import (
    FrontendGatewayConfig,
    create_frontend_gateway,
)


class _Upstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.response_status = 200
        self.response_body: bytes | None = None
        self.response_headers: list[tuple[str, str]] = []
        self.response_delay = 0.0
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)


class _UpstreamHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def _respond(self) -> None:
        time.sleep(self.server.response_delay)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )
        response = (
            {
                "schema_version": "open_trader.legacy_dashboard.health.v1",
                "module": "legacy_dashboard",
            }
            if self.path == "/healthz"
            else {"ok": True}
        )
        encoded = (
            self.server.response_body
            if self.server.response_body is not None
            else json.dumps(response).encode()
        )
        self.send_response(self.server.response_status)
        if not any(name.lower() == "content-type" for name, _ in self.server.response_headers):
            self.send_header("Content-Type", "application/json")
        for name, value in self.server.response_headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _running(server: ThreadingHTTPServer) -> Iterator[ThreadingHTTPServer]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_static_files(static_dir: Path) -> dict[str, bytes]:
    files = {
        "index.html": b"<h1>Open Trader</h1>",
        "dashboard.css": b"body { color: navy; }",
        "dashboard.js": b"window.openTrader = true;",
    }
    static_dir.mkdir()
    for name, body in files.items():
        (static_dir / name).write_bytes(body)
    return files


@contextmanager
def _gateway(
    static_dir: Path,
    upstream_port: int,
    *,
    timeout: float = 1.0,
    max_request_body_bytes: int = 20 * 1024 * 1024,
) -> Iterator[str]:
    server = create_frontend_gateway(
        config=FrontendGatewayConfig(
            static_dir=static_dir,
            upstream_port=upstream_port,
            public_origin="http://127.0.0.1:8766",
            upstream_timeout_seconds=timeout,
            max_request_body_bytes=max_request_body_bytes,
        ),
        host="127.0.0.1",
        port=0,
    )
    with _running(server):
        yield f"http://127.0.0.1:{server.server_address[1]}"


def test_gateway_serves_dashboard_assets_without_contacting_upstream(
    tmp_path: Path,
) -> None:
    expected = _write_static_files(tmp_path / "static")
    upstream = _Upstream()
    with _running(upstream), _gateway(
        tmp_path / "static", upstream.server_address[1]
    ) as base:
        routes = {
            "/": expected["index.html"],
            "/static/dashboard.css": expected["dashboard.css"],
            "/static/dashboard.js": expected["dashboard.js"],
        }
        for route, body in routes.items():
            with urllib.request.urlopen(base + route, timeout=5) as response:
                assert response.status == 200
                assert response.read() == body

    assert upstream.requests == []


def test_gateway_rejects_unknown_non_api_path(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    upstream = _Upstream()
    with _running(upstream), _gateway(
        tmp_path / "static", upstream.server_address[1]
    ) as base:
        try:
            urllib.request.urlopen(base + "/admin", timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("unknown route was accepted")

    assert upstream.requests == []


def test_gateway_health_reports_runtime_and_upstream_status(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    upstream = _Upstream()
    with _running(upstream), _gateway(
        tmp_path / "static", upstream.server_address[1]
    ) as base:
        with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
            payload = json.load(response)

    assert payload["schema_version"] == "open_trader.frontend_gateway.health.v1"
    assert payload["module"] == "frontend_gateway"
    assert payload["upstream_status"] == "ok"
    assert isinstance(payload["pid"], int)
    assert payload["cwd"]
    assert upstream.requests[0]["path"] == "/healthz"


def test_gateway_forwards_api_get_path_query_status_and_body(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    upstream = _Upstream()
    upstream.response_status = 202
    upstream.response_body = b'{"legacy":true}'
    upstream.response_headers = [("Content-Type", "application/vnd.legacy+json")]
    with _running(upstream), _gateway(
        tmp_path / "static", upstream.server_address[1]
    ) as base:
        with urllib.request.urlopen(
            base + "/api/items?limit=2&kind=a", timeout=5
        ) as response:
            assert response.code == 202
            assert response.headers["Content-Type"] == "application/vnd.legacy+json"
            assert response.read() == b'{"legacy":true}'

    assert upstream.requests[0]["method"] == "GET"
    assert upstream.requests[0]["path"] == "/api/items?limit=2&kind=a"


def test_gateway_forwards_api_post_body_cookie_csrf_and_trusted_origin(
    tmp_path: Path,
) -> None:
    _write_static_files(tmp_path / "static")
    upstream = _Upstream()
    body = b'{"preview_id":"p1"}'
    request = urllib.request.Request(
        "http://unused/api/prediction-arbitrage/executions",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Cookie": "ot_prediction_session=session",
            "Origin": "http://127.0.0.1:8766",
            "Referer": "http://127.0.0.1:8766/prediction?tab=ready",
            "X-CSRF-Token": "csrf",
        },
    )
    with _running(upstream), _gateway(
        tmp_path / "static", upstream.server_address[1]
    ) as base:
        request.full_url = base + "/api/prediction-arbitrage/executions"
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200

    recorded = upstream.requests[0]
    headers = recorded["headers"]
    upstream_origin = f"http://127.0.0.1:{upstream.server_address[1]}"
    assert recorded["body"] == body
    assert headers["Host"] == f"127.0.0.1:{upstream.server_address[1]}"
    assert headers["Origin"] == upstream_origin
    assert headers["Referer"] == upstream_origin + "/prediction?tab=ready"
    assert headers["Cookie"] == "ot_prediction_session=session"
    assert headers["X-Csrf-Token"] == "csrf"


def test_gateway_does_not_launder_an_untrusted_origin(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    upstream = _Upstream()
    request = urllib.request.Request(
        "http://unused/api/prediction-arbitrage/preview",
        data=b"{}",
        method="POST",
        headers={"Origin": "https://attacker.example"},
    )
    with _running(upstream), _gateway(
        tmp_path / "static", upstream.server_address[1]
    ) as base:
        request.full_url = base + "/api/prediction-arbitrage/preview"
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 403

    assert upstream.requests == []


def test_gateway_keeps_originless_loopback_clients_compatible(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    upstream = _Upstream()
    request = urllib.request.Request(
        "http://unused/api/backtests/standard/run",
        data=b"{}",
        method="POST",
    )
    with _running(upstream), _gateway(
        tmp_path / "static", upstream.server_address[1]
    ) as base:
        request.full_url = base + "/api/backtests/standard/run"
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200

    assert "Origin" not in upstream.requests[0]["headers"]


def test_gateway_passes_every_set_cookie_to_the_browser(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    upstream = _Upstream()
    upstream.response_headers = [
        ("Set-Cookie", "session=one; Path=/"),
        ("Set-Cookie", "csrf=two; Path=/"),
    ]
    with _running(upstream), _gateway(
        tmp_path / "static", upstream.server_address[1]
    ) as base:
        connection = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
        connection.request("GET", "/api/session")
        response = connection.getresponse()
        response.read()
        cookies = [
            value for name, value in response.getheaders() if name.lower() == "set-cookie"
        ]
        connection.close()

    assert cookies == ["session=one; Path=/", "csrf=two; Path=/"]


def test_gateway_strips_hop_by_hop_headers_in_both_directions(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    upstream = _Upstream()
    upstream.response_headers = [
        ("Connection", "X-Internal"),
        ("X-Internal", "secret"),
        ("Keep-Alive", "timeout=5"),
    ]
    with _running(upstream), _gateway(
        tmp_path / "static", upstream.server_address[1]
    ) as base:
        connection = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
        connection.putrequest("GET", "/api/state")
        connection.putheader("Connection", "X-Remove")
        connection.putheader("X-Remove", "secret")
        connection.putheader("Keep-Alive", "timeout=1")
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        connection.close()

    request_headers = {
        name.lower(): value for name, value in upstream.requests[0]["headers"].items()
    }
    assert "connection" not in request_headers
    assert "x-remove" not in request_headers
    assert "keep-alive" not in request_headers
    assert "connection" not in response_headers
    assert "x-internal" not in response_headers
    assert "keep-alive" not in response_headers


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({}, 400),
        ({"Content-Length": "invalid"}, 400),
        ({"Content-Length": "5"}, 413),
        ({"Transfer-Encoding": "chunked"}, 400),
    ],
)
def test_gateway_rejects_invalid_or_oversized_request_bodies(
    tmp_path: Path,
    headers: dict[str, str],
    expected_status: int,
) -> None:
    _write_static_files(tmp_path / "static")
    upstream = _Upstream()
    with _running(upstream), _gateway(
        tmp_path / "static",
        upstream.server_address[1],
        max_request_body_bytes=4,
    ) as base:
        connection = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
        connection.putrequest("POST", "/api/upload")
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        if headers.get("Transfer-Encoding") == "chunked":
            connection.send(b"0\r\n\r\n")
        response = connection.getresponse()
        response.read()
        connection.close()

    assert response.status == expected_status
    assert upstream.requests == []


def test_gateway_returns_structured_503_when_upstream_is_unavailable(
    tmp_path: Path,
) -> None:
    _write_static_files(tmp_path / "static")
    socket_holder = socket.socket()
    socket_holder.bind(("127.0.0.1", 0))
    unavailable_port = socket_holder.getsockname()[1]
    socket_holder.close()
    with _gateway(tmp_path / "static", unavailable_port, timeout=0.05) as base:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/api/state", timeout=5)
        payload = json.load(error.value)

    assert error.value.code == 503
    assert payload == {
        "schema_version": "open_trader.frontend_gateway.error.v1",
        "code": "legacy_dashboard_unavailable",
        "message": "Legacy Dashboard is unavailable",
    }


def test_gateway_returns_503_when_upstream_times_out(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    upstream = _Upstream()
    upstream.response_delay = 0.2
    with _running(upstream), _gateway(
        tmp_path / "static", upstream.server_address[1], timeout=0.01
    ) as base:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/api/state", timeout=5)
        error.value.read()

    assert error.value.code == 503
