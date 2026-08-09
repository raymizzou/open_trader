"""Frozen Prediction API baseline and approved service semantics."""

from typing import Final


PREDICTION_API_CONTRACT_V1: Final[dict[str, object]] = {
    "version": 1,
    "legacy_baseline": {
        "owner": "legacy_dashboard",
        "routes": {
            "/api/prediction-arbitrage/state": {
                "method": "GET",
                "success_status": 200,
            },
            "/api/prediction-arbitrage/history": {
                "method": "GET",
                "success_status": 200,
            },
            "/api/prediction-arbitrage/preview": {
                "method": "POST",
                "fields": ("opportunity_id",),
                "success_status": 200,
            },
            "/api/prediction-arbitrage/executions": {
                "method": "POST",
                "fields": ("preview_id", "idempotency_key"),
                "success_status": 200,
            },
            "/api/prediction-arbitrage/mode": {
                "method": "POST",
                "fields": ("mode",),
                "success_status": 200,
            },
            "/api/prediction-arbitrage/circuit-breaker/reset": {
                "method": "POST",
                "fields": ("incident_id",),
                "success_status": 200,
            },
            "/api/prediction-arbitrage/predict-allowance/cleanup": {
                "method": "POST",
                "fields": ("confirm",),
                "success_status": 200,
            },
            "/api/prediction-arbitrage/cross-auto/pause": {
                "method": "POST",
                "fields": ("confirm",),
                "success_status": 200,
            },
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
            "OBSERVE_MANUAL": {
                "manual_submit": True,
                "automatic_submit": False,
            },
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
        "liveness": {
            "endpoint": "/healthz",
            "status": 200,
            "implies_order_ready": False,
        },
        "not_ready": {"state_status": 503, "mutation_status": 503},
        "source_degraded": {
            "state_status": 200,
            "affected_source_order_ready": False,
        },
        "history_when_ledger_readable": {"status": 200},
        "unknown_or_stale": {"proof_status": "UNKNOWN", "order_ready": False},
    },
}
