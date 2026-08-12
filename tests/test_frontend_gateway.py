from __future__ import annotations

from contextlib import contextmanager
import http.client
from http import HTTPStatus
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


_PREDICTION_ROUTE_SCHEMA = "open_trader.frontend_gateway.prediction_route.v1"


class _Upstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.response_status = 200
        self.response_reason: str | None = None
        self.response_body: bytes | None = None
        self.response_headers: list[tuple[str, str]] = []
        self.response_delay = 0.0
        self.health_body: bytes | None = None
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
        response = {"ok": True}
        encoded = (
            self.server.health_body
            if self.path == "/healthz" and self.server.health_body is not None
            else self.server.response_body
            if self.server.response_body is not None
            else json.dumps(response).encode()
        )
        if self.path == "/healthz" and self.server.health_body is None:
            encoded = json.dumps(
                {
                    "schema_version": "open_trader.legacy_dashboard.health.v1",
                    "module": "legacy_dashboard",
                }
            ).encode()
        self.send_response(self.server.response_status, self.server.response_reason)
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


def _write_route(path: Path, mode: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": _PREDICTION_ROUTE_SCHEMA,
                "mode": mode,
                "operation_id": "operation-1",
                "updated_at": "2026-08-12T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def _prediction_request(base: str, method: str, path: str) -> None:
    connection = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
    connection.request(method, path, body=b"{}" if method == "POST" else None)
    response = connection.getresponse()
    response.read()
    assert response.status == HTTPStatus.OK
    connection.close()


@contextmanager
def _gateway(
    static_dir: Path,
    upstream_port: int,
    account_upstream_port: int | None = None,
    *,
    prediction_port: int | None = None,
    prediction_route_path: Path | None = None,
    timeout: float = 1.0,
    max_request_body_bytes: int = 20 * 1024 * 1024,
) -> Iterator[str]:
    route = prediction_route_path or static_dir.parent / "prediction-route.json"
    if prediction_route_path is None:
        _write_route(route, "legacy")
    server = create_frontend_gateway(
        config=FrontendGatewayConfig(
            static_dir=static_dir,
            upstream_port=upstream_port,
            account_upstream_port=account_upstream_port or upstream_port,
            prediction_route_path=route,
            prediction_upstream_port=prediction_port or upstream_port,
            public_origin="http://127.0.0.1:8766",
            upstream_timeout_seconds=timeout,
            max_request_body_bytes=max_request_body_bytes,
        ),
        host="127.0.0.1",
        port=0,
    )
    with _running(server):
        yield f"http://127.0.0.1:{server.server_address[1]}"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_prediction_prefix_routes_as_one_unit(tmp_path: Path, method: str) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    prediction = _Upstream()
    route = tmp_path / "prediction-route.json"
    _write_route(route, "service")
    with _running(legacy), _running(prediction), _gateway(
        tmp_path / "static",
        legacy.server_address[1],
        prediction_port=prediction.server_address[1],
        prediction_route_path=route,
    ) as base:
        _prediction_request(base, method, "/api/prediction-arbitrage/state")

    assert legacy.requests == []
    assert [item["path"] for item in prediction.requests] == [
        "/api/prediction-arbitrage/state"
    ]


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_prediction_prefix_legacy_mode_routes_as_one_unit(
    tmp_path: Path, method: str
) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    prediction = _Upstream()
    route = tmp_path / "prediction-route.json"
    _write_route(route, "legacy")
    with _running(legacy), _running(prediction), _gateway(
        tmp_path / "static",
        legacy.server_address[1],
        prediction_port=prediction.server_address[1],
        prediction_route_path=route,
    ) as base:
        _prediction_request(base, method, "/api/prediction-arbitrage/state")

    assert [item["path"] for item in legacy.requests] == [
        "/api/prediction-arbitrage/state"
    ]
    assert prediction.requests == []


def test_prediction_route_keeps_nonprefix_and_account_routes_unchanged(
    tmp_path: Path,
) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    account = _Upstream()
    prediction = _Upstream()
    route = tmp_path / "prediction-route.json"
    _write_route(route, "service")
    with _running(legacy), _running(account), _running(prediction), _gateway(
        tmp_path / "static",
        legacy.server_address[1],
        account.server_address[1],
        prediction_port=prediction.server_address[1],
        prediction_route_path=route,
    ) as base:
        _prediction_request(base, "GET", "/api/prediction-arbitragex")
        _prediction_request(base, "GET", "/api/v1/account/snapshot")

    assert [item["path"] for item in legacy.requests] == [
        "/api/prediction-arbitragex"
    ]
    assert [item["path"] for item in account.requests] == ["/api/v1/account/snapshot"]
    assert prediction.requests == []


def test_prediction_service_preserves_headers_body_and_response(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    prediction = _Upstream()
    route = tmp_path / "prediction-route.json"
    _write_route(route, "service")
    prediction.response_status = HTTPStatus.ACCEPTED
    prediction.response_reason = "Prediction Accepted"
    prediction.response_body = b"prediction bytes"
    prediction.response_headers = [
        ("ETag", '"prediction-v1"'),
        ("Set-Cookie", "prediction=one; Path=/"),
    ]
    body = b'{"preview_id":"p1"}'
    with _running(legacy), _running(prediction), _gateway(
        tmp_path / "static",
        legacy.server_address[1],
        prediction_port=prediction.server_address[1],
        prediction_route_path=route,
    ) as base:
        connection = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
        connection.putrequest("POST", "/api/prediction-arbitrage/executions")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        connection.putheader("Cookie", "ot_prediction_session=session")
        connection.putheader("Origin", "http://127.0.0.1:8766")
        connection.putheader("Referer", "http://127.0.0.1:8766/prediction")
        connection.putheader("X-CSRF-Token", "csrf")
        connection.endheaders(body)
        response = connection.getresponse()
        response_body = response.read()
        response_headers = response.getheaders()
        connection.close()

    prediction_origin = f"http://127.0.0.1:{prediction.server_address[1]}"
    assert (response.status, response.reason, response_body) == (
        HTTPStatus.ACCEPTED,
        "Prediction Accepted",
        b"prediction bytes",
    )
    assert ("ETag", '"prediction-v1"') in response_headers
    assert ("Set-Cookie", "prediction=one; Path=/") in response_headers
    assert prediction.requests[0]["body"] == body
    headers = {
        name.lower(): value for name, value in prediction.requests[0]["headers"].items()
    }
    assert headers["host"] == f"127.0.0.1:{prediction.server_address[1]}"
    assert headers["origin"] == prediction_origin
    assert headers["referer"] == prediction_origin + "/prediction"
    assert headers["cookie"] == "ot_prediction_session=session"
    assert headers["x-csrf-token"] == "csrf"
    assert legacy.requests == []


def test_prediction_route_health_reports_selected_service_status(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    prediction = _Upstream()
    prediction.health_body = json.dumps({"module": "prediction_service"}).encode()
    route = tmp_path / "prediction-route.json"
    _write_route(route, "service")
    with _running(legacy), _running(prediction), _gateway(
        tmp_path / "static",
        legacy.server_address[1],
        prediction_port=prediction.server_address[1],
        prediction_route_path=route,
    ) as base:
        with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
            payload = json.load(response)

    assert payload["prediction_route_mode"] == "service"
    assert payload["prediction_inflight_requests"] == 0
    assert payload["prediction_upstream_status"] == "ok"


def test_prediction_untrusted_origin_releases_inflight_request(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    prediction = _Upstream()
    route = tmp_path / "prediction-route.json"
    _write_route(route, "service")
    with _running(legacy), _running(prediction), _gateway(
        tmp_path / "static",
        legacy.server_address[1],
        prediction_port=prediction.server_address[1],
        prediction_route_path=route,
    ) as base:
        request = urllib.request.Request(
            base + "/api/prediction-arbitrage/executions",
            data=b"{}",
            method="POST",
            headers={"Origin": "https://attacker.example"},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == HTTPStatus.FORBIDDEN
        with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
            health = json.load(response)

    assert health["prediction_inflight_requests"] == 0
    assert [request["path"] for request in prediction.requests] == ["/healthz"]


def test_prediction_health_handles_non_object_json_upstream_body(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    prediction = _Upstream()
    prediction.health_body = b"[]"
    route = tmp_path / "prediction-route.json"
    _write_route(route, "service")
    with _running(legacy), _running(prediction), _gateway(
        tmp_path / "static",
        legacy.server_address[1],
        prediction_port=prediction.server_address[1],
        prediction_route_path=route,
    ) as base:
        with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
            health = json.load(response)

    assert health["prediction_upstream_status"] == "unavailable"


@pytest.mark.parametrize("record", [None, "{", "unknown", "maintenance"])
def test_prediction_route_maintenance_rejects_before_body_or_upstream(
    tmp_path: Path, record: str | None
) -> None:
    _write_static_files(tmp_path / "static")
    route = tmp_path / "prediction-route.json"
    if record == "{":
        route.write_text(record, encoding="utf-8")
    elif record is not None:
        _write_route(route, record)
    legacy = _Upstream()
    prediction = _Upstream()
    with _running(legacy), _running(prediction), _gateway(
        tmp_path / "static",
        legacy.server_address[1],
        prediction_port=prediction.server_address[1],
        prediction_route_path=route,
    ) as base:
        connection = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
        connection.putrequest("POST", "/api/prediction-arbitrage/executions")
        connection.putheader("Content-Length", "1")
        connection.endheaders()
        connection.sock.shutdown(socket.SHUT_WR)
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert payload == {
        "schema_version": "open_trader.frontend_gateway.error.v1",
        "code": "prediction_maintenance",
        "message": "Prediction service is in maintenance",
        "route_mode": "maintenance",
    }
    assert legacy.requests == []
    assert prediction.requests == []


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


def test_gateway_routes_only_the_exact_account_snapshot_path(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    account = _Upstream()
    with _running(legacy), _running(account), _gateway(
        tmp_path / "static", legacy.server_address[1], account.server_address[1]
    ) as base:
        for route in ("/api/v1/account/snapshot", "/api/v1/account/snapshot?fresh=1"):
            with urllib.request.urlopen(base + route, timeout=5) as response:
                assert response.status == 200
        for route in (
            "/api/v1/account/snapshot/child",
            "/api/dashboard",
            "/api/quotes",
            "/api/statements",
            "/api/simulate",
            "/api/anything-else",
        ):
            with urllib.request.urlopen(base + route, timeout=5) as response:
                assert response.status == 200

    assert [request["path"] for request in account.requests] == [
        "/api/v1/account/snapshot",
        "/api/v1/account/snapshot?fresh=1",
    ]
    assert [request["path"] for request in legacy.requests] == [
        "/api/v1/account/snapshot/child",
        "/api/dashboard",
        "/api/quotes",
        "/api/statements",
        "/api/simulate",
        "/api/anything-else",
    ]


@pytest.mark.parametrize(
    ("status", "reason", "body"),
    [
        (200, "Account Ready", b'{"account":true}'),
        (304, "Account Not Modified", b""),
        (503, "Account Contract Unavailable", b'{"code":"contract_unavailable"}'),
    ],
)
def test_gateway_preserves_account_response_details(
    tmp_path: Path,
    status: int,
    reason: str,
    body: bytes,
) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    account = _Upstream()
    account.response_status = status
    account.response_reason = reason
    account.response_body = body
    account.response_headers = [
        ("ETag", '"account-v1"'),
        ("X-Account-Trace", "one"),
        ("X-Account-Trace", "two"),
    ]
    with _running(legacy), _running(account), _gateway(
        tmp_path / "static", legacy.server_address[1], account.server_address[1]
    ) as base:
        connection = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
        connection.request("GET", "/api/v1/account/snapshot")
        response = connection.getresponse()
        response_body = response.read()
        response_headers = response.getheaders()
        connection.close()

    assert (response.status, response.reason, response_body) == (status, reason, body)
    assert ("ETag", '"account-v1"') in response_headers
    assert [value for name, value in response_headers if name == "X-Account-Trace"] == [
        "one",
        "two",
    ]
    assert legacy.requests == []


def test_gateway_marks_and_rewrites_account_requests_without_legacy_authority(
    tmp_path: Path,
) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    account = _Upstream()
    with _running(legacy), _running(account), _gateway(
        tmp_path / "static", legacy.server_address[1], account.server_address[1]
    ) as base:
        connection = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
        connection.putrequest("GET", "/api/v1/account/snapshot")
        connection.putheader("Origin", "http://127.0.0.1:8766")
        connection.putheader("Referer", "http://127.0.0.1:8766/dashboard")
        connection.putheader("X-Open-Trader-Account-Route", "caller-value")
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        connection.close()

    headers = account.requests[0]["headers"]
    account_origin = f"http://127.0.0.1:{account.server_address[1]}"
    assert headers["X-Open-Trader-Account-Route"] == "production"
    assert headers["Origin"] == account_origin
    assert headers["Referer"] == account_origin + "/dashboard"
    assert legacy.requests == []


def test_gateway_transparently_routes_statement_command_to_account(
    tmp_path: Path,
) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    account = _Upstream()
    account.response_status = HTTPStatus.ACCEPTED
    account.response_body = b'{"status":"staged","statement_generation":"sha256:one"}'
    body = b"%PDF-1.7\nstatement"
    with _running(legacy), _running(account), _gateway(
        tmp_path / "static", legacy.server_address[1], account.server_address[1]
    ) as base:
        request = urllib.request.Request(
            base + "/api/v1/account/statements/phillips",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/pdf",
                "Origin": "http://127.0.0.1:8766",
            },
        )
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
            status = response.status

    assert status == HTTPStatus.ACCEPTED
    assert payload["statement_generation"] == "sha256:one"
    assert account.requests[0]["path"] == "/api/v1/account/statements/phillips"
    assert account.requests[0]["body"] == body
    assert account.requests[0]["headers"]["X-Open-Trader-Account-Route"] == "production"
    assert legacy.requests == []


def test_gateway_reports_account_and_legacy_health_independently(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    account = _Upstream()
    account.health_body = json.dumps(
        {"module": "account_api", "mode": "production"}
    ).encode()
    with _running(legacy), _running(account), _gateway(
        tmp_path / "static", legacy.server_address[1], account.server_address[1]
    ) as base:
        with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
            payload = json.load(response)

    assert payload["upstream_status"] == "ok"
    assert payload["legacy_upstream_status"] == "ok"
    assert payload["account_upstream_status"] == "ok"


def test_gateway_health_keeps_legacy_and_account_failures_independent(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    account = _Upstream()
    account.health_body = json.dumps({"module": "account_api", "mode": "shadow"}).encode()
    with _running(legacy), _running(account), _gateway(
        tmp_path / "static", legacy.server_address[1], account.server_address[1]
    ) as base:
        with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
            shadow_payload = json.load(response)
    assert shadow_payload["legacy_upstream_status"] == "ok"
    assert shadow_payload["account_upstream_status"] == "unavailable"

    socket_holder = socket.socket()
    socket_holder.bind(("127.0.0.1", 0))
    unavailable_port = socket_holder.getsockname()[1]
    socket_holder.close()
    account = _Upstream()
    account.health_body = json.dumps(
        {"module": "account_api", "mode": "production"}
    ).encode()
    with _running(account), _gateway(
        tmp_path / "static", unavailable_port, account.server_address[1], timeout=0.05
    ) as base:
        with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
            unavailable_payload = json.load(response)
    assert unavailable_payload["upstream_status"] == "unavailable"
    assert unavailable_payload["legacy_upstream_status"] == "unavailable"
    assert unavailable_payload["account_upstream_status"] == "ok"

    legacy = _Upstream()
    socket_holder = socket.socket()
    socket_holder.bind(("127.0.0.1", 0))
    unavailable_port = socket_holder.getsockname()[1]
    socket_holder.close()
    with _running(legacy), _gateway(
        tmp_path / "static", legacy.server_address[1], unavailable_port, timeout=0.05
    ) as base:
        with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
            unavailable_account_payload = json.load(response)
    assert unavailable_account_payload["legacy_upstream_status"] == "ok"
    assert unavailable_account_payload["account_upstream_status"] == "unavailable"


def test_gateway_returns_account_503_without_using_legacy(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    legacy = _Upstream()
    socket_holder = socket.socket()
    socket_holder.bind(("127.0.0.1", 0))
    unavailable_port = socket_holder.getsockname()[1]
    socket_holder.close()
    with _running(legacy), _gateway(
        tmp_path / "static", legacy.server_address[1], unavailable_port, timeout=0.05
    ) as base:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/api/v1/account/snapshot", timeout=5)
        payload = json.load(error.value)

    assert error.value.code == 503
    assert payload == {
        "schema_version": "open_trader.frontend_gateway.error.v1",
        "code": "account_module_unavailable",
        "message": "Account module is unavailable",
    }
    assert legacy.requests == []


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


def test_gateway_rewrites_only_the_configured_public_origin(tmp_path: Path) -> None:
    _write_static_files(tmp_path / "static")
    upstream = _Upstream()
    request = urllib.request.Request(
        "http://unused/api/state",
        headers={"Origin": "http://127.0.0.1:9999"},
    )
    with _running(upstream), _gateway(
        tmp_path / "static", upstream.server_address[1]
    ) as base:
        request.full_url = base + "/api/state"
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200

    assert upstream.requests[0]["headers"]["Origin"] == "http://127.0.0.1:9999"


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
