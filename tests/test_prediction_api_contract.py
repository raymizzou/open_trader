from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from typing import Iterator
import urllib.error
import urllib.request

import pytest

from open_trader.dashboard_web import create_dashboard_server
from open_trader.frontend_gateway import FrontendGatewayConfig, create_frontend_gateway
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_service import create_prediction_server
from tests.test_dashboard import dashboard_config
from tests.test_prediction_arbitrage_execution import (
    ChannelNotifier,
    CompositeTestNotifier,
    FakeTrading,
    execution_fixture,
    wait_until_terminal,
)


class _Store:
    def active_execution(self) -> None:
        return None

    def unacknowledged_incident(self) -> None:
        return None

    def signal_history(self, _window: str) -> list[object]:
        return []

    def histories(self, _kind: str) -> list[object]:
        return []

    def get_validation_mode(self) -> str:
        return "manual"

    def auto_eat_stats(self) -> dict[str, object]:
        return {}


class _Monitor:
    def snapshot(self) -> dict[str, object]:
        return {
            "status": "healthy",
            "health": {"status": "healthy", "degraded_reasons": []},
            "readiness": {
                "status": "ready",
                "geoblock": "allowed",
                "relayer": "ready",
            },
            "heartbeat_at": "2026-08-10T00:00:00Z",
            "stale": False,
            "events": [],
            "opportunities": [],
        }


class _Execution:
    _breaker_open = False
    _cross_breaker_open = False

    def preview(self, opportunity_id: str) -> dict[str, object]:
        return {
            "state": "previewed",
            "id": "preview-1",
            "preview_id": "preview-1",
            "opportunity_id": opportunity_id,
            "api_secret": "must-not-escape",
        }

    def confirm(self, preview_id: str, idempotency_key: str) -> dict[str, object]:
        return {
            "state": "validating",
            "execution_id": "execution-1",
            "preview_id": preview_id,
            "idempotency_key": idempotency_key,
        }

    def set_validation_mode(
        self, mode: str, *, audit: object | None = None
    ) -> dict[str, object]:
        return {"state": "ok", "mode": mode}

    def reset_breaker(
        self, incident_id: str, *, audit: object | None = None
    ) -> dict[str, object]:
        return {
            "state": "ready",
            "reason": "reset_confirmed",
            "incident_id": incident_id,
        }

    def cleanup_predict_allowance(
        self, *, confirm: bool, audit: object | None = None
    ) -> dict[str, object]:
        assert confirm is True
        return {
            "state": "ready",
            "before_allowance": "1",
            "after_allowance": "0",
            "usdt_moved": False,
        }

    def pause_cross_auto(self, *, audit: object | None = None) -> dict[str, object]:
        return {
            "configured_mode": "manual_confirm",
            "armed": False,
            "reason": "operator_paused",
            "updated_at": "2026-08-10T00:00:00Z",
        }

    def cross_auto_status(self) -> dict[str, object]:
        return {
            "configured_mode": "manual_confirm",
            "effective_mode": "manual_confirm",
            "armed": False,
        }


CONTROL_CASES = (
    (
        "/api/prediction-arbitrage/mode",
        {"mode": "manual"},
        {"state": "ok", "mode": "manual"},
    ),
    (
        "/api/prediction-arbitrage/circuit-breaker/reset",
        {"incident_id": "incident-1"},
        {
            "state": "ready",
            "reason": "reset_confirmed",
            "incident_id": "incident-1",
        },
    ),
    (
        "/api/prediction-arbitrage/predict-allowance/cleanup",
        {"confirm": True},
        {
            "state": "ready",
            "before_allowance": "1",
            "after_allowance": "0",
            "usdt_moved": False,
        },
    ),
    (
        "/api/prediction-arbitrage/cross-auto/pause",
        {"confirm": True},
        {
            "configured_mode": "manual_confirm",
            "armed": False,
            "reason": "operator_paused",
            "updated_at": "2026-08-10T00:00:00Z",
        },
    ),
)


class _ProductionRuntime:
    state = "RUNNING"
    mode = "production"
    production_owner = True

    def __init__(self) -> None:
        self.store = _Store()
        self.monitor = _Monitor()
        self.execution = _Execution()
        self.cross_venue_monitor = None


@contextmanager
def _serve(server: object) -> Iterator[str]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)  # type: ignore[attr-defined]
    thread.start()
    try:
        host, port = server.server_address  # type: ignore[attr-defined]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()  # type: ignore[attr-defined]
        server.server_close()  # type: ignore[attr-defined]
        thread.join(timeout=5)


def _record_requests(
    server: object, handler_name: str, requests: list[dict[str, object]]
) -> None:
    handler = server.RequestHandlerClass  # type: ignore[attr-defined]

    class RecordingHandler(handler):
        def _record(self) -> None:
            requests.append(
                {
                    "handler": handler_name,
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                }
            )

        def do_GET(self) -> None:
            self._record()
            super().do_GET()

        def do_POST(self) -> None:
            self._record()
            super().do_POST()

    server.RequestHandlerClass = RecordingHandler  # type: ignore[attr-defined]


@contextmanager
def _gateway_contract_stack(
    tmp_path: Path,
    *,
    store: object | None = None,
    monitor: object | None = None,
    execution: object | None = None,
) -> Iterator[tuple[str, dict[str, object]]]:
    store = store or _Store()
    monitor = monitor or _Monitor()
    execution = execution or _Execution()
    legacy = create_dashboard_server(
        config=dashboard_config(tmp_path),
        host="127.0.0.1",
        port=0,
        prediction_store=store,
        prediction_monitor=monitor,
        prediction_execution_service=execution,
        prediction_session_token="session-token",
        prediction_csrf_token="csrf-token",
    )
    runtime = _ProductionRuntime()
    runtime.store = store
    runtime.monitor = monitor
    runtime.execution = execution
    service = create_prediction_server(
        runtime=runtime,  # type: ignore[arg-type]
        port=0,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    )
    upstream_requests: dict[str, list[dict[str, object]]] = {
        "legacy": [],
        "service": [],
    }
    _record_requests(legacy, "legacy", upstream_requests["legacy"])
    _record_requests(service, "service", upstream_requests["service"])
    route = tmp_path / "prediction-route.json"
    route.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.frontend_gateway.prediction_route.v1",
                "mode": "legacy",
                "operation_id": "contract-parity",
                "updated_at": "2026-08-12T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    with _serve(legacy) as legacy_base, _serve(service) as service_base:
        gateway = create_frontend_gateway(
            config=FrontendGatewayConfig(
                static_dir=tmp_path / "static",
                upstream_port=int(legacy_base.rsplit(":", 1)[1]),
                account_upstream_port=int(legacy_base.rsplit(":", 1)[1]),
                prediction_route_path=route,
                prediction_upstream_port=int(service_base.rsplit(":", 1)[1]),
                public_origin="http://127.0.0.1:8766",
            ),
            host="127.0.0.1",
            port=0,
        )
        with _serve(gateway) as gateway_base:
            yield gateway_base, {
                "route": route,
                "requests": upstream_requests,
                "origins": {"legacy": legacy_base, "service": service_base},
                "runtime": runtime,
            }


@contextmanager
def _prediction_server() -> Iterator[str]:
    server = create_prediction_server(
        runtime=_ProductionRuntime(),  # type: ignore[arg-type]
        port=0,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
    )
    with _serve(server) as base:
        yield base


@contextmanager
def _dashboard_server(tmp_path: Path, *, available: bool) -> Iterator[str]:
    server = create_dashboard_server(
        config=dashboard_config(tmp_path),
        host="127.0.0.1",
        port=0,
        prediction_store=_Store() if available else None,
        prediction_monitor=_Monitor() if available else None,
        prediction_execution_service=_Execution() if available else None,
        prediction_session_token="session-token",
        prediction_csrf_token="csrf-token",
    )
    with _serve(server) as base:
        yield base


def _json_response(request: str | urllib.request.Request) -> tuple[int, object, object]:
    with urllib.request.urlopen(request, timeout=5) as response:
        return (
            response.status,
            response.headers,
            json.loads(response.read().decode("utf-8")),
        )


def _post(
    base: str,
    path: str,
    payload: dict[str, object],
    *,
    csrf_token: str = "csrf-token",
    session_token: str = "session-token",
    origin: str | None = None,
    referer: str | None = None,
) -> urllib.request.Request:
    return urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Cookie": f"ot_prediction_session={session_token}",
            "Origin": origin or base,
            **({"Referer": referer} if referer is not None else {}),
            "X-CSRF-Token": csrf_token,
        },
        method="POST",
    )


def _gateway_post(
    base: str,
    path: str,
    payload: dict[str, object],
    *,
    csrf_token: str = "csrf-token",
    session_token: str = "session-token",
    origin: str = "http://127.0.0.1:8766",
) -> urllib.request.Request:
    return _post(
        base,
        path,
        payload,
        csrf_token=csrf_token,
        session_token=session_token,
        origin=origin,
        referer=origin + "/prediction",
    )


def _strict_equal(actual: object, expected: object, path: str = "$") -> None:
    assert type(actual) is type(expected), (path, type(actual), type(expected))
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        assert set(actual) == set(expected), path
        for key in expected:
            _strict_equal(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list)
        assert len(actual) == len(expected), path
        for index, value in enumerate(expected):
            _strict_equal(actual[index], value, f"{path}[{index}]")
    else:
        assert actual == expected, path


def test_strict_contract_comparison_rejects_bool_as_int() -> None:
    with pytest.raises(AssertionError):
        _strict_equal({"nested": [True]}, {"nested": [1]})


def _durable_snapshot(store: PredictionArbitrageStore) -> dict[str, object]:
    executions = store.histories("executions")
    with sqlite3.connect(store.path) as connection:
        table_counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("executions", "execution_legs", "incidents", "control_events")
        }
        mode_transitions = connection.execute(
            "SELECT count(*) FROM control_events WHERE action='set_validation_mode'"
        ).fetchone()[0]
    return {
        "executions": executions,
        "table_counts": table_counts,
        "evidence_count": sum(len(item["evidence"]) for item in executions),
        "validation_mode": store.get_validation_mode(),
        "mode_transitions": mode_transitions,
    }


def _response_observation(
    request: str | urllib.request.Request,
) -> tuple[int, dict[str, object], object]:
    try:
        status, headers, payload = _json_response(request)
    except urllib.error.HTTPError as error:
        status = error.code
        headers = error.headers
        payload = json.loads(error.read().decode("utf-8"))
    return (
        status,
        {
            "content-type": headers.get("Content-Type"),
            "set-cookie": headers.get_all("Set-Cookie", []),
        },
        payload,
    )


def _contract_observations(base: str) -> dict[str, tuple[int, dict[str, object], object]]:
    requests: tuple[tuple[str, str | urllib.request.Request], ...] = (
        ("state", base + "/api/prediction-arbitrage/state"),
        (
            "signal_history",
            base
            + "/api/prediction-arbitrage/history?kind=signals&limit=20&offset=0",
        ),
        (
            "execution_history",
            base
            + "/api/prediction-arbitrage/history?kind=executions&limit=20&offset=0",
        ),
        (
            "preview",
            _gateway_post(
                base,
                "/api/prediction-arbitrage/preview",
                {"opportunity_id": "opp-1"},
            ),
        ),
        (
            "execution",
            _gateway_post(
                base,
                "/api/prediction-arbitrage/executions",
                {"preview_id": "preview-1", "idempotency_key": "key-1"},
            ),
        ),
        *(
            (path, _gateway_post(base, path, payload))
            for path, payload, _expected in CONTROL_CASES
        ),
        (
            "schema_error",
            _gateway_post(
                base,
                "/api/prediction-arbitrage/preview",
                {"opportunity_id": "opp-1", "unexpected": True},
            ),
        ),
    )
    observed: dict[str, tuple[int, dict[str, object], object]] = {}
    for name, request in requests:
        observed[name] = _response_observation(request)
    return observed


def test_gateway_preserves_the_frozen_prediction_contract_across_cutover(
    tmp_path: Path,
) -> None:
    matrix_paths = [
        "/api/prediction-arbitrage/state",
        "/api/prediction-arbitrage/history?kind=signals&limit=20&offset=0",
        "/api/prediction-arbitrage/history?kind=executions&limit=20&offset=0",
        "/api/prediction-arbitrage/preview",
        "/api/prediction-arbitrage/executions",
        *(path for path, _payload, _expected in CONTROL_CASES),
    ]
    with _gateway_contract_stack(tmp_path) as (base, stack):
        route = stack["route"]
        upstream_requests = stack["requests"]
        legacy = _contract_observations(base)
        assert upstream_requests["service"] == []
        legacy_requests = list(upstream_requests["legacy"])
        assert [request["handler"] for request in legacy_requests] == [
            "legacy"
        ] * len(legacy_requests)
        assert [request["path"] for request in legacy_requests[:9]] == matrix_paths
        route.write_text(
            json.dumps(
                {
                    "schema_version": "open_trader.frontend_gateway.prediction_route.v1",
                    "mode": "service",
                    "operation_id": "contract-parity",
                    "updated_at": "2026-08-12T00:01:00Z",
                }
            ),
            encoding="utf-8",
        )
        service = _contract_observations(base)
        assert upstream_requests["legacy"] == legacy_requests
        service_requests = upstream_requests["service"]
        assert [request["handler"] for request in service_requests] == [
            "service"
        ] * len(service_requests)
        assert [request["path"] for request in service_requests[:9]] == matrix_paths

        for handler_name, requests in (
            ("legacy", legacy_requests),
            ("service", service_requests),
        ):
            target_origin = stack["origins"][handler_name]
            for request in requests:
                if request["method"] != "POST":
                    continue
                headers = request["headers"]
                assert headers["Cookie"] == "ot_prediction_session=session-token"
                assert headers["X-Csrf-Token"] == "csrf-token"
                assert headers["Origin"] == target_origin
                assert headers["Referer"] == target_origin + "/prediction"

        security: dict[str, dict[str, tuple[int, dict[str, object], object]]] = {}
        for route_mode in ("legacy", "service"):
            route.write_text(
                json.dumps(
                    {
                        "schema_version": "open_trader.frontend_gateway.prediction_route.v1",
                        "mode": route_mode,
                        "operation_id": "security-parity",
                        "updated_at": "2026-08-12T00:02:00Z",
                    }
                ),
                encoding="utf-8",
            )
            security[route_mode] = {
                "wrong_session": _response_observation(
                    _gateway_post(
                        base,
                        "/api/prediction-arbitrage/preview",
                        {"opportunity_id": "opp-1"},
                        session_token="wrong",
                    )
                ),
                "wrong_csrf": _response_observation(
                    _gateway_post(
                        base,
                        "/api/prediction-arbitrage/preview",
                        {"opportunity_id": "opp-1"},
                        csrf_token="wrong",
                    )
                ),
            }
        before_wrong_origin = {
            name: len(requests) for name, requests in upstream_requests.items()
        }
        wrong_origin = _response_observation(
            _gateway_post(
                base,
                "/api/prediction-arbitrage/preview",
                {"opportunity_id": "opp-1"},
                origin="https://attacker.example",
            )
        )

    _strict_equal(service, legacy)
    assert {name: result[0] for name, result in service.items()} == {
        "state": 200,
        "signal_history": 200,
        "execution_history": 200,
        "preview": 200,
        "execution": 200,
        **{path: 200 for path, _payload, _expected in CONTROL_CASES},
        "schema_error": 400,
    }
    assert all(
        result[1]["content-type"] == "application/json; charset=utf-8"
        for result in service.values()
    )
    assert service["state"][1]["set-cookie"] == [
        "ot_prediction_session=session-token; SameSite=Strict; HttpOnly; Path=/"
    ]
    assert all(
        result[1]["set-cookie"] == []
        for name, result in service.items()
        if name != "state"
    )
    assert service["signal_history"][2] == {
        "kind": "signals",
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
        "has_more": False,
    }
    assert service["execution_history"][2] == {
        "kind": "executions",
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
        "has_more": False,
    }
    assert service["schema_error"][2] == {
        "status": "error",
        "error_type": "ValueError",
        "message": "prediction request fields are invalid",
    }
    assert "api_secret" not in service["preview"][2]
    _strict_equal(security["service"], security["legacy"])
    assert security["service"]["wrong_session"] == (
        403,
        {"content-type": "application/json; charset=utf-8", "set-cookie": []},
        {
            "status": "error",
            "error_type": "PermissionError",
            "message": "prediction session is invalid",
        },
    )
    assert security["service"]["wrong_csrf"] == (
        403,
        {"content-type": "application/json; charset=utf-8", "set-cookie": []},
        {
            "status": "error",
            "error_type": "PermissionError",
            "message": "prediction CSRF token is invalid",
        },
    )
    assert wrong_origin == (
        403,
        {"content-type": "application/json; charset=utf-8", "set-cookie": []},
        {
            "schema_version": "open_trader.frontend_gateway.error.v1",
            "code": "untrusted_origin",
            "message": "Origin is not trusted",
        },
    )
    assert {
        name: len(requests) for name, requests in upstream_requests.items()
    } == before_wrong_origin
    for result in (*service.values(), *security["service"].values(), wrong_origin):
        text = json.dumps(result[2])
        assert "session-token" not in text
        assert "must-not-escape" not in text
        assert "Traceback" not in text


def test_gateway_cutover_reuses_one_durable_execution_without_duplicate_audit(
    tmp_path: Path,
) -> None:
    execution, trading, store, monitor = execution_fixture(
        tmp_path, result="both_rejected"
    )
    request_body = {"preview_id": "", "idempotency_key": "cutover-request"}
    with _gateway_contract_stack(
        tmp_path,
        store=store,
        monitor=monitor,
        execution=execution,
    ) as (base, stack):
        route = stack["route"]
        _status, _headers, preview = _json_response(
            _gateway_post(
                base,
                "/api/prediction-arbitrage/preview",
                {"opportunity_id": "opp-1"},
            )
        )
        request_body["preview_id"] = str(preview["preview_id"])
        first_status, _headers, first = _json_response(
            _gateway_post(base, "/api/prediction-arbitrage/executions", request_body)
        )
        terminal = wait_until_terminal(execution, str(first["execution_id"]))
        assert terminal["state"] == "both_rejected"
        before_restart = _durable_snapshot(store)
        assert trading.batch_calls == 1

        route.write_text(
            json.dumps(
                {
                    "schema_version": "open_trader.frontend_gateway.prediction_route.v1",
                    "mode": "maintenance",
                    "operation_id": "idempotency-cutover",
                    "updated_at": "2026-08-12T00:02:00Z",
                }
            ),
            encoding="utf-8",
        )
        maintenance_statuses = []
        for _attempt in range(2):
            try:
                _json_response(
                    _gateway_post(
                        base, "/api/prediction-arbitrage/executions", request_body
                    )
                )
            except urllib.error.HTTPError as error:
                maintenance_statuses.append(error.code)

        restarted_store = PredictionArbitrageStore(store.data_dir)
        restarted_trading = FakeTrading(result="both_rejected")
        restarted_execution = PredictionExecutionService(
            store=restarted_store,
            monitor=monitor,
            trading=restarted_trading,
            notifier=CompositeTestNotifier(
                ChannelNotifier("macos"), ChannelNotifier("feishu")
            ),
            lock_path=tmp_path / "execution.lock",
        )
        assert restarted_execution.reconcile_startup()["state"] == "ready"
        stack["runtime"].store = restarted_store
        stack["runtime"].execution = restarted_execution

        route.write_text(
            json.dumps(
                {
                    "schema_version": "open_trader.frontend_gateway.prediction_route.v1",
                    "mode": "service",
                    "operation_id": "idempotency-cutover",
                    "updated_at": "2026-08-12T00:03:00Z",
                }
            ),
            encoding="utf-8",
        )
        retry_status, _headers, retry = _json_response(
            _gateway_post(base, "/api/prediction-arbitrage/executions", request_body)
        )
        history_status, _headers, history = _json_response(
            base
            + "/api/prediction-arbitrage/history?kind=executions&limit=20&offset=0"
        )

    after_restart = _durable_snapshot(restarted_store)
    assert first_status == retry_status == 200
    assert maintenance_statuses == [503, 503]
    assert retry["execution_id"] == first["execution_id"]
    assert history_status == 200
    assert history["total"] == 1
    assert [item["execution_id"] for item in history["items"]] == [
        first["execution_id"]
    ]
    assert before_restart == after_restart
    assert after_restart["table_counts"] == {
        "executions": 1,
        "execution_legs": 2,
        "incidents": 0,
        "control_events": 0,
    }
    assert after_restart["executions"][0]["idempotency_key"] == "cutover-request"
    assert restarted_trading.batch_calls == 0


def test_legacy_prediction_http_surface_matches_contract_v1(tmp_path: Path) -> None:
    with _dashboard_server(tmp_path, available=True) as base:
        status, headers, state = _json_response(base + "/api/prediction-arbitrage/state")
        assert status == 200
        assert "ot_prediction_session=session-token" in headers["Set-Cookie"]
        assert "SameSite=Strict" in headers["Set-Cookie"]
        assert "HttpOnly" in headers["Set-Cookie"]
        assert state["csrf_token"] == "csrf-token"
        assert {
            "status", "health", "readiness", "stale", "events", "opportunities",
            "venues", "cross_venue", "relation_discovery", "validation_mode", "cross_auto",
            "current_execution", "breaker",
        } <= set(state)

        status, _headers, history = _json_response(
            base + "/api/prediction-arbitrage/history?kind=signals&limit=1&offset=0"
        )
        assert status == 200
        assert history == {
            "kind": "signals", "items": [], "total": 0,
            "limit": 1, "offset": 0, "has_more": False,
        }

        cases = (
            (
                "/api/prediction-arbitrage/preview",
                {"opportunity_id": "opp-1"},
                {
                    "state": "previewed",
                    "id": "preview-1",
                    "preview_id": "preview-1",
                    "opportunity_id": "opp-1",
                },
            ),
            (
                "/api/prediction-arbitrage/executions",
                {"preview_id": "preview-1", "idempotency_key": "key-1"},
                {
                    "state": "validating",
                    "execution_id": "execution-1",
                    "preview_id": "preview-1",
                    "idempotency_key": "key-1",
                },
            ),
            *CONTROL_CASES,
        )
        for path, payload, expected in cases:
            status, _headers, body = _json_response(_post(base, path, payload))
            assert status == 200, path
            assert body == expected, path

        unauthorized = _post(base, "/api/prediction-arbitrage/preview", {"opportunity_id": "opp-1"})
        del unauthorized.headers["X-csrf-token"]
        try:
            urllib.request.urlopen(unauthorized, timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 403
        else:
            raise AssertionError("missing CSRF must fail")

        invalid = _post(
            base,
            "/api/prediction-arbitrage/preview",
            {"opportunity_id": "opp-1", "unexpected": True},
        )
        try:
            urllib.request.urlopen(invalid, timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 400
        else:
            raise AssertionError("unexpected request fields must fail")


def test_production_prediction_mutations_match_frozen_legacy_contract() -> None:
    with _prediction_server() as base:
        status, headers, state = _json_response(
            base + "/api/prediction-arbitrage/state"
        )
        assert status == 200
        assert "ot_prediction_session=session-token" in headers["Set-Cookie"]
        assert "SameSite=Strict" in headers["Set-Cookie"]
        assert "HttpOnly" in headers["Set-Cookie"]
        assert state["csrf_token"] == "csrf-token"

        for path, payload, expected in CONTROL_CASES:
            status, _headers, body = _json_response(_post(base, path, payload))
            assert status == 200, path
            assert body == expected, path

        status, _headers, preview = _json_response(
            _post(
                base,
                "/api/prediction-arbitrage/preview",
                {"opportunity_id": "opp-1"},
            )
        )
        assert status == 200
        assert preview == {
            "state": "previewed",
            "id": "preview-1",
            "preview_id": "preview-1",
            "opportunity_id": "opp-1",
        }

        status, _headers, execution = _json_response(
            _post(
                base,
                "/api/prediction-arbitrage/executions",
                {"preview_id": "preview-1", "idempotency_key": "key-1"},
            )
        )
        assert status == 200
        assert execution == {
            "state": "validating",
            "execution_id": "execution-1",
            "preview_id": "preview-1",
            "idempotency_key": "key-1",
        }


def test_legacy_unavailable_state_is_the_documented_migration_gap(tmp_path: Path) -> None:
    with _dashboard_server(tmp_path, available=False) as base:
        status, _headers, state = _json_response(base + "/api/prediction-arbitrage/state")
        try:
            urllib.request.urlopen(
                _post(base, "/api/prediction-arbitrage/preview", {"opportunity_id": "opp-1"}),
                timeout=5,
            )
        except urllib.error.HTTPError as error:
            mutation_status = error.code
            mutation_error = json.loads(error.read().decode("utf-8"))
        else:
            raise AssertionError("unavailable execution service must fail")

    assert status == 200
    assert state["status"] == "unavailable"
    assert state["readiness"]["status"] == "unavailable"
    assert state["stale"] is True
    assert state["breaker"]["open"] is True
    assert mutation_status == 500
    assert mutation_error["status"] == "error"
    assert mutation_error["error_type"] == "RuntimeError"
