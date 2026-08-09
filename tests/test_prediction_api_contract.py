from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
from typing import Iterator
import urllib.error
import urllib.request

from open_trader.dashboard_web import create_dashboard_server
from open_trader.prediction_api_contract import PREDICTION_API_CONTRACT_V1
from tests.test_dashboard import dashboard_config


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
        return {"operation": "preview", "opportunity_id": opportunity_id}

    def confirm(self, preview_id: str, idempotency_key: str) -> dict[str, object]:
        return {
            "operation": "execution",
            "preview_id": preview_id,
            "idempotency_key": idempotency_key,
        }

    def set_validation_mode(self, mode: str) -> dict[str, object]:
        return {"operation": "mode", "mode": mode}

    def reset_breaker(self, incident_id: str) -> dict[str, object]:
        return {"operation": "breaker_reset", "incident_id": incident_id}

    def cleanup_predict_allowance(self, *, confirm: bool) -> dict[str, object]:
        return {"operation": "allowance_cleanup", "confirmed": confirm}

    def pause_cross_auto(self) -> dict[str, object]:
        return {"operation": "cross_auto_pause"}

    def cross_auto_status(self) -> dict[str, object]:
        return {
            "configured_mode": "manual_confirm",
            "effective_mode": "manual_confirm",
            "armed": False,
        }


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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _json_response(request: str | urllib.request.Request) -> tuple[int, object, object]:
    with urllib.request.urlopen(request, timeout=5) as response:
        return (
            response.status,
            response.headers,
            json.loads(response.read().decode("utf-8")),
        )


def _post(base: str, path: str, payload: dict[str, object]) -> urllib.request.Request:
    return urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Cookie": "ot_prediction_session=session-token",
            "Origin": base,
            "X-CSRF-Token": "csrf-token",
        },
        method="POST",
    )


def test_prediction_platform_contract_v1_is_explicit() -> None:
    assert PREDICTION_API_CONTRACT_V1 == {
        "version": 1,
        "legacy_baseline": {
            "owner": "legacy_dashboard",
            "routes": {
                "/api/prediction-arbitrage/state": {"method": "GET", "success_status": 200},
                "/api/prediction-arbitrage/history": {"method": "GET", "success_status": 200},
                "/api/prediction-arbitrage/preview": {"method": "POST", "fields": ("opportunity_id",), "success_status": 200},
                "/api/prediction-arbitrage/executions": {"method": "POST", "fields": ("preview_id", "idempotency_key"), "success_status": 200},
                "/api/prediction-arbitrage/mode": {"method": "POST", "fields": ("mode",), "success_status": 200},
                "/api/prediction-arbitrage/circuit-breaker/reset": {"method": "POST", "fields": ("incident_id",), "success_status": 200},
                "/api/prediction-arbitrage/predict-allowance/cleanup": {"method": "POST", "fields": ("confirm",), "success_status": 200},
                "/api/prediction-arbitrage/cross-auto/pause": {"method": "POST", "fields": ("confirm",), "success_status": 200},
            },
            "history_kinds": ("signals", "executions", "incidents"),
            "validation_modes": ("observe_only", "manual", "auto"),
            "cross_auto_modes": ("observe_only", "manual_confirm", "auto_submit"),
            "unavailable_state_status": 200,
        },
        "product": {
            "strategy_type_cardinality": "exactly_one",
            "strategy_types": ("YES_NO", "LLM_RELATION", "N_LEG"),
            "strategy_mode_scope": "per_strategy_type",
            "strategy_modes": {
                "OBSERVE_MANUAL": {"manual_submit": True, "automatic_submit": False},
                "AUTO": {"manual_submit": True, "automatic_submit": True},
            },
            "shared_safety_standard": True,
            "submit_requires": (
                "approved_relation",
                "current_proof",
                "fresh_quotes",
                "positive_guaranteed_profit",
                "depth",
                "balance",
                "risk_limits",
                "global_breaker_closed",
            ),
            "n_leg_initial_mode": "OBSERVE_MANUAL",
            "global_breaker": {
                "blocks": ("manual_submit", "automatic_submit", "automatic_repair"),
                "allows": ("market_data", "discovery", "proof", "display", "history"),
            },
        },
        "prediction_service_target": {
            "owner": "prediction_service",
            "liveness": {"endpoint": "/healthz", "status": 200, "implies_order_ready": False},
            "not_ready": {"state_status": 503, "mutation_status": 503},
            "source_degraded": {"state_status": 200, "affected_source_order_ready": False},
            "history_when_ledger_readable": {"status": 200},
            "unknown_or_stale": {"proof_status": "UNKNOWN", "order_ready": False},
        },
    }


def test_legacy_prediction_http_surface_matches_contract_v1(tmp_path: Path) -> None:
    with _dashboard_server(tmp_path, available=True) as base:
        status, headers, state = _json_response(base + "/api/prediction-arbitrage/state")
        assert status == 200
        assert "ot_prediction_session=session-token" in headers["Set-Cookie"]
        assert "SameSite=Strict" in headers["Set-Cookie"]
        assert "HttpOnly" in headers["Set-Cookie"]
        assert state["csrf_token"] == "csrf-token"
        assert {
            "status", "health", "readiness", "stale", "opportunities", "validation_mode",
            "cross_auto", "current_execution", "breaker",
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
            ("/api/prediction-arbitrage/preview", {"opportunity_id": "opp-1"}, "preview"),
            ("/api/prediction-arbitrage/executions", {"preview_id": "preview-1", "idempotency_key": "key-1"}, "execution"),
            ("/api/prediction-arbitrage/mode", {"mode": "manual"}, "mode"),
            ("/api/prediction-arbitrage/circuit-breaker/reset", {"incident_id": "incident-1"}, "breaker_reset"),
            ("/api/prediction-arbitrage/predict-allowance/cleanup", {"confirm": True}, "allowance_cleanup"),
            ("/api/prediction-arbitrage/cross-auto/pause", {"confirm": True}, "cross_auto_pause"),
        )
        for path, payload, operation in cases:
            status, _headers, body = _json_response(_post(base, path, payload))
            assert status == 200, path
            assert body["operation"] == operation, path

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


def test_legacy_unavailable_state_is_the_documented_migration_gap(tmp_path: Path) -> None:
    with _dashboard_server(tmp_path, available=False) as base:
        status, _headers, state = _json_response(base + "/api/prediction-arbitrage/state")

    assert status == 200
    assert state["status"] == "unavailable"
    assert state["readiness"]["status"] == "unavailable"
    assert state["stale"] is True
    assert state["breaker"]["open"] is True
