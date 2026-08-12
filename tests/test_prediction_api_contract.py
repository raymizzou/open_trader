from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
from typing import Iterator
import urllib.error
import urllib.request

from open_trader.dashboard_web import create_dashboard_server
from open_trader.frontend_gateway import FrontendGatewayConfig, create_frontend_gateway
from open_trader.prediction_service import create_prediction_server
from tests.test_dashboard import dashboard_config
from tests.test_prediction_arbitrage_execution import execution_fixture, wait_until_terminal


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


@contextmanager
def _gateway_contract_stack(
    tmp_path: Path,
    *,
    store: object | None = None,
    monitor: object | None = None,
    execution: object | None = None,
) -> Iterator[tuple[str, Path]]:
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
            yield gateway_base, route


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
    origin: str | None = None,
) -> urllib.request.Request:
    return urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Cookie": "ot_prediction_session=session-token",
            "Origin": origin or base,
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
) -> urllib.request.Request:
    return _post(
        base,
        path,
        payload,
        csrf_token=csrf_token,
        origin="http://127.0.0.1:8766",
    )


def _contract_observations(base: str) -> dict[str, tuple[int, object]]:
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
            "execution_retry",
            _gateway_post(
                base,
                "/api/prediction-arbitrage/executions",
                {"preview_id": "preview-1", "idempotency_key": "key-1"},
            ),
        ),
        (
            "security_error",
            _gateway_post(
                base,
                "/api/prediction-arbitrage/preview",
                {"opportunity_id": "opp-1"},
                csrf_token="wrong",
            ),
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
    observed: dict[str, tuple[int, object]] = {}
    for name, request in requests:
        try:
            status, _headers, payload = _json_response(request)
        except urllib.error.HTTPError as error:
            status = error.code
            payload = json.loads(error.read().decode("utf-8"))
        observed[name] = status, payload
    return observed


def test_gateway_preserves_the_frozen_prediction_contract_across_cutover(
    tmp_path: Path,
) -> None:
    with _gateway_contract_stack(tmp_path) as (base, route):
        legacy = _contract_observations(base)
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

    assert service == legacy
    assert {name: status for name, (status, _body) in service.items()} == {
        "state": 200,
        "signal_history": 200,
        "execution_history": 200,
        "preview": 200,
        "execution": 200,
        **{path: 200 for path, _payload, _expected in CONTROL_CASES},
        "execution_retry": 200,
        "security_error": 403,
        "schema_error": 400,
    }
    assert service["signal_history"][1] == {
        "kind": "signals",
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
        "has_more": False,
    }
    assert service["execution_history"][1] == {
        "kind": "executions",
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
        "has_more": False,
    }
    assert service["execution_retry"] == service["execution"]
    for name in ("security_error", "schema_error"):
        error_text = json.dumps(service[name][1])
        assert "session-token" not in error_text
        assert "Traceback" not in error_text


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
    ) as (base, route):
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
        wait_until_terminal(execution, str(first["execution_id"]))

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

    executions = store.histories("executions")
    assert first_status == retry_status == 200
    assert maintenance_statuses == [503, 503]
    assert retry["execution_id"] == first["execution_id"]
    assert history_status == 200
    assert history["total"] == 1
    assert [item["execution_id"] for item in history["items"]] == [
        first["execution_id"]
    ]
    assert len(executions) == 1
    assert executions[0]["idempotency_key"] == "cutover-request"
    assert trading.batch_calls == 1
    assert store.get_validation_mode() == "observe_only"


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
