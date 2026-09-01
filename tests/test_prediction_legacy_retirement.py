"""Issue #60 phase-A slice 2: legacy retirement at reader fence >= 2.

The SAME code must be dual-state: at fence 1 the legacy endpoints keep the
frozen contract behavior; at fence 2 (``N_LEG_READER_GENERATION``) the legacy
write endpoints answer 410 ``legacy_strategy_removed``, the legacy auto paths
stay disarmed, and the opportunities list is owned by N_LEG solution
projections. Expected values come from the approved issue #60 slice plan.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
import threading
from typing import Iterator
import urllib.error
import urllib.request

import pytest

from open_trader.prediction_market_solution import MarketSolution
from open_trader.prediction_n_leg import ActionQuantity, canonical_payload, fingerprint
from open_trader.prediction_read_model import prediction_state_payload
from open_trader.prediction_service import create_prediction_server
from open_trader.relation_catalog import RelationCatalog
from test_relation_catalog import compiled_problem, discovery


LEGACY_STRATEGY_REMOVED = "legacy_strategy_removed"

FIVE_LEGACY_POSTS = (
    ("/api/prediction-arbitrage/preview", {"opportunity_id": "opp-1"}),
    (
        "/api/prediction-arbitrage/executions",
        {"preview_id": "preview-1", "idempotency_key": "key-1"},
    ),
    ("/api/prediction-arbitrage/mode", {"mode": "manual"}),
    (
        "/api/prediction-arbitrage/circuit-breaker/reset",
        {"incident_id": "incident-1"},
    ),
    ("/api/prediction-arbitrage/cross-auto/pause", {"confirm": True}),
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

    def cross_auto_state(self) -> dict[str, object]:
        return {"configured_mode": "manual_confirm", "armed": False}

    def cross_auto_attempts(self, limit: int = 1) -> list[object]:
        return []

    def cross_auto_daily_principal(self) -> object:
        return "0"


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
    """Legacy mutation surface; every retired entrypoint must never be hit."""

    _breaker_open = False
    _cross_breaker_open = False

    def preview(self, opportunity_id: str) -> dict[str, object]:
        raise AssertionError("retired preview endpoint reached the execution")

    def confirm(self, preview_id: str, idempotency_key: str) -> dict[str, object]:
        raise AssertionError("retired executions endpoint reached the execution")

    def set_validation_mode(
        self, mode: str, *, audit: object | None = None
    ) -> dict[str, object]:
        raise AssertionError("retired mode endpoint reached the execution")

    def reset_breaker(
        self, incident_id: str, *, audit: object | None = None
    ) -> dict[str, object]:
        raise AssertionError("retired breaker endpoint reached the execution")

    def pause_cross_auto(self, *, audit: object | None = None) -> dict[str, object]:
        raise AssertionError("retired cross-auto endpoint reached the execution")

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

    def cross_auto_status(self) -> dict[str, object]:
        return {
            "configured_mode": "manual_confirm",
            "effective_mode": "manual_confirm",
            "armed": False,
        }

    def n_leg_mode_contract(self) -> dict[str, object]:
        return {
            "schema_version": "open_trader.prediction_n_leg.mode_contract.v1",
            "contract_generation": 2,
            "mode": "MANUAL",
            "qualification_policy_version": 1,
            "qualification_policy": {},
            "safety_config_version": 1,
            "safety_config": {},
            "execution_scopes": {},
            "enabled_execution_scope_version": [],
            "execution_gates": {
                "breaker_open": False,
                "incident_active": False,
                "batch_active": False,
            },
        }


class _FakeRuntime:
    state = "RUNNING"
    mode = "production"
    production_owner = True

    def __init__(self, *, legacy_retired: bool) -> None:
        self.legacy_retired = legacy_retired
        self.store = _Store()
        self.monitor = _Monitor()
        self.execution = _Execution()
        self.cross_venue_monitor = None


class _ContractExecution:
    """N_LEG mode contract with one MANUAL_CANARY scope (fixture pattern of
    tests/test_prediction_read_model.py::test_nleg_solution_projection_...)."""

    _breaker_open = False
    _cross_breaker_open = False

    def n_leg_mode_contract(self) -> dict[str, object]:
        return {
            "schema_version": "open_trader.prediction_n_leg.mode_contract.v1",
            "contract_generation": 2,
            "mode": "MANUAL",
            "qualification_policy_version": 1,
            "qualification_policy": {},
            "safety_config_version": 1,
            "safety_config": {
                "episode_rearm_gap_seconds": 300,
                "max_total_unsettled_capital_units": 60_000_000,
                "max_partial_fill_loss_units": 0,
                "max_auto_repair_loss_units": 0,
            },
            "execution_scopes": {
                "s1": {
                    "scope_id": "s1",
                    "capability": "MANUAL_CANARY",
                    "scope_version": 1,
                },
            },
            "enabled_execution_scope_version": [{"scope_id": "s1", "scope_version": 1}],
            "execution_gates": {
                "breaker_open": False,
                "incident_active": False,
                "batch_active": False,
            },
        }


@contextmanager
def _serve(runtime: _FakeRuntime) -> Iterator[str]:
    server = create_prediction_server(
        runtime=runtime,  # type: ignore[arg-type]
        port=0,
        session_token="session-token",
        csrf_token="csrf-token",
        runtime_metadata={"git_sha": "abc123"},
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


def _post_json(
    base: str, path: str, payload: dict[str, object]
) -> tuple[int, object]:
    request = urllib.request.Request(
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
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_fence2_retired_runtime_answers_410_on_five_legacy_post_endpoints() -> None:
    with _serve(_FakeRuntime(legacy_retired=True)) as base:
        for path, payload in FIVE_LEGACY_POSTS:
            status, body = _post_json(base, path, payload)
            assert status == 410, path
            assert body["error_code"] == LEGACY_STRATEGY_REMOVED, path


def _post_json_unauthenticated(
    base: str, path: str, payload: dict[str, object]
) -> tuple[int, object]:
    """POST with no session cookie, no Origin, and no CSRF token — exactly
    what the orchestrator's live 410 probe (scripts/run_nleg_cutover.py)
    sends during post-verify."""

    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_fence2_retired_runtime_answers_410_before_production_auth() -> None:
    """Issue #60 final-review P1-1: the retired-mode legacy POSTs answer 410
    UNIFORMLY, so an unauthenticated request (the cutover probe carries no
    session) must reach the 410 gate instead of being stopped by production
    auth with 403. Expected values: the ticket's legacy-removal semantic."""

    with _serve(_FakeRuntime(legacy_retired=True)) as base:
        status, body = _post_json_unauthenticated(
            base, "/api/prediction-arbitrage/mode", {"mode": "manual"}
        )

    assert status == 410
    assert body["error_code"] == LEGACY_STRATEGY_REMOVED


def test_fence1_unauthenticated_mode_post_still_requires_production_auth() -> None:
    """Issue #60 final-review P1-1 frozen-ordering pin: at fence 1 the 410
    gate is inert, so an unauthenticated legacy POST is still stopped by
    production auth with 403 — the reorder must not weaken the auth gate."""

    with _serve(_FakeRuntime(legacy_retired=False)) as base:
        status, body = _post_json_unauthenticated(
            base, "/api/prediction-arbitrage/mode", {"mode": "manual"}
        )

    assert status == 403
    assert body["error_type"] == "PermissionError"


def test_fence2_retired_runtime_keeps_non_legacy_post_endpoints_alive() -> None:
    with _serve(_FakeRuntime(legacy_retired=True)) as base:
        status, body = _post_json(
            base,
            "/api/prediction-arbitrage/predict-allowance/cleanup",
            {"confirm": True},
        )
        assert status == 200
        assert body == {
            "state": "ready",
            "before_allowance": "1",
            "after_allowance": "0",
            "usdt_moved": False,
        }


class _LegacyExecution(_Execution):
    """Fence-1 mutation surface: the frozen legacy contract responses."""

    def preview(self, opportunity_id: str) -> dict[str, object]:
        return {
            "state": "previewed",
            "id": "preview-1",
            "preview_id": "preview-1",
            "opportunity_id": opportunity_id,
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

    def pause_cross_auto(self, *, audit: object | None = None) -> dict[str, object]:
        return {
            "configured_mode": "manual_confirm",
            "armed": False,
            "reason": "operator_paused",
            "updated_at": "2026-08-10T00:00:00Z",
        }

    def n_leg_mode_contract(self) -> dict[str, object]:
        return {
            "schema_version": "open_trader.prediction_n_leg.mode_contract.v1",
            "contract_generation": 1,
            "mode": "MANUAL",
            "qualification_policy_version": 1,
            "qualification_policy": {},
            "safety_config_version": 1,
            "safety_config": {},
            "execution_scopes": {},
            "enabled_execution_scope_version": [],
            "execution_gates": {
                "breaker_open": False,
                "incident_active": False,
                "batch_active": False,
            },
        }


FROZEN_FENCE1_RESPONSES = (
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
    ("/api/prediction-arbitrage/mode", {"mode": "manual"}, {"state": "ok", "mode": "manual"}),
    (
        "/api/prediction-arbitrage/circuit-breaker/reset",
        {"incident_id": "incident-1"},
        {"state": "ready", "reason": "reset_confirmed", "incident_id": "incident-1"},
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


def test_fence1_runtime_still_serves_frozen_legacy_contract() -> None:
    runtime = _FakeRuntime(legacy_retired=False)
    runtime.execution = _LegacyExecution()
    with _serve(runtime) as base:
        for path, payload, expected in FROZEN_FENCE1_RESPONSES:
            status, body = _post_json(base, path, payload)
            assert status == 200, path
            assert body == expected, path


def test_fence1_mode_still_accepts_the_legacy_mode_payload() -> None:
    runtime = _FakeRuntime(legacy_retired=False)
    runtime.execution = _LegacyExecution()
    with _serve(runtime) as base:
        status, body = _post_json(
            base, "/api/prediction-arbitrage/mode", {"mode": "auto"}
        )
        assert status == 200
        assert body == {"state": "ok", "mode": "auto"}


class _RetiredStateStore(_Store):
    def llm_usage_24h(self) -> dict[str, object]:
        return {"calls": 0, "successes": 0, "failures": 0, "cache_hits": 0}

    def cross_unsettled_principal(self) -> object:
        return "0"


class _RetiredStateMonitor:
    """A healthy legacy monitor: at fence 2 its rows must not be used."""

    def snapshot(self) -> dict[str, object]:
        return {
            "status": "healthy",
            "health": {"status": "healthy", "degraded_reasons": []},
            "readiness": {
                "status": "ready",
                "geoblock": "allowed",
                "relayer": "ready",
                "wallet_address": "0x1111111111111111111111111111111111111111",
                "p_usd_balance": "60.40",
                "p_usd_allowance": "60.40",
            },
            "heartbeat_at": "2026-08-10T00:00:00Z",
            "stale": False,
            "events": [],
            "opportunities": [
                {
                    "opportunity_id": "legacy-1",
                    "market_type": "standard_binary",
                    "question": "Legacy signal-derived row",
                    "actionable": True,
                    "profit": "1.00",
                }
            ],
        }


def test_fence2_state_payload_opportunities_are_all_n_leg_owned() -> None:
    market = canonical_payload(
        MarketSolution(
            component_id="c1",
            structure_fingerprint="sha256:struct",
            quote_fingerprint="sha256:quote",
            quantities=(ActionQuantity("a-yes", 20), ActionQuantity("a-no", 20)),
            guaranteed_profit_units=8_400_000,
            bounded_cost_units=31_200_000,
            bounded_payout_units=39_600_000,
            capital_release_at=datetime(2026, 8, 30, tzinfo=UTC),
            global_search_closed=False,
            verification_fingerprint="sha256:verify",
        )
    )
    state = prediction_state_payload(
        store=_RetiredStateStore(),
        monitor=_RetiredStateMonitor(),
        execution=_ContractExecution(),
        csrf_token="csrf",
        n_leg_solutions=[
            {
                "component_id": "c1",
                "scope_id": "s1",
                "market": market,
                "execution": {
                    "market_solution_fingerprint": fingerprint(
                        canonical_payload(market)
                    ),
                    "quantities": market["quantities"],
                    "capital_use_units": 31_200_000,
                    "reason": "EXECUTABLE",
                    "order_ready": False,
                    "partial_fill_proof": "PARTIAL_FILL_SAFE",
                },
            }
        ],
        legacy_retired=True,
    )

    opportunities = state["opportunities"]
    assert opportunities, "the seeded N_LEG solution must surface as an opportunity"
    assert all(
        row["engine_owner"] == "N_LEG" for row in opportunities
    )
    assert [row["component_id"] for row in opportunities] == ["c1"]


def test_fence2_state_payload_opportunities_are_empty_without_solutions() -> None:
    state = prediction_state_payload(
        store=_RetiredStateStore(),
        monitor=_RetiredStateMonitor(),
        execution=_ContractExecution(),
        csrf_token="csrf",
        legacy_retired=True,
    )

    assert state["opportunities"] == []


TAXONOMY_RELEASE_AT = "2026-12-31T17:00:00Z"


def _implies_relation_discovery(
    contract_a: str = "cond-a", contract_b: str = "cond-b"
) -> dict[str, object]:
    """One IMPLIES relation over a same-venue, same-event contract pair."""
    payload = discovery(
        relation_type="IMPLIES",
        n=2,
        venues=("polymarket", "polymarket"),
        event_bases=("event-btc-1", "event-btc-1"),
        problem=compiled_problem(
            [contract_a, contract_b],
            {contract_a: "BUY_YES", contract_b: "BUY_NO"},
            release_at=TAXONOMY_RELEASE_AT,
        ),
    )
    payload["markets"][0]["contract_id"] = contract_a
    payload["markets"][1]["contract_id"] = contract_b
    payload["model"]["capital_release"] = TAXONOMY_RELEASE_AT
    return payload


def _native_complement_relation_discovery() -> dict[str, object]:
    """One NATIVE_COMPLEMENT relation over cat-b/cat-c.

    Shares the IMPLIES relation's event basis: the activation gate publishes
    only same-event generations, which is exactly the merged display case.
    """
    payload = discovery(
        relation_type="NATIVE_COMPLEMENT",
        n=2,
        venues=("polymarket", "polymarket"),
        event_bases=("event-btc-1", "event-btc-1"),
        problem=compiled_problem(
            ["cat-b", "cat-c"],
            # cat-b keeps the BUY_NO side of the cat implies relation: the
            # activation gate merges compiled problems per action id, so a
            # shared contract must not carry conflicting payouts.
            {"cat-b": "BUY_NO", "cat-c": "BUY_YES"},
            release_at=TAXONOMY_RELEASE_AT,
        ),
    )
    payload["markets"][0]["contract_id"] = "cat-b"
    payload["markets"][1]["contract_id"] = "cat-c"
    payload["model"]["capital_release"] = TAXONOMY_RELEASE_AT
    return payload


def _taxonomy_component_solution(
    component_id: str, contract_ids: list[str]
) -> dict[str, object]:
    market = canonical_payload(
        MarketSolution(
            component_id=component_id,
            structure_fingerprint="sha256:struct",
            quote_fingerprint="sha256:quote",
            quantities=tuple(
                ActionQuantity(f"polymarket:{contract_id}", 20)
                for contract_id in contract_ids
            ),
            guaranteed_profit_units=8_400_000,
            bounded_cost_units=31_200_000,
            bounded_payout_units=39_600_000,
            capital_release_at=datetime(2026, 12, 31, tzinfo=UTC),
            global_search_closed=False,
            verification_fingerprint="sha256:verify",
        )
    )
    return {
        "component_id": component_id,
        "scope_id": "s1",
        "market": market,
        "execution": {
            "market_solution_fingerprint": fingerprint(canonical_payload(market)),
            "quantities": market["quantities"],
            "capital_use_units": 31_200_000,
            "reason": "EXECUTABLE",
            "order_ready": False,
            "partial_fill_proof": "PARTIAL_FILL_SAFE",
        },
    }


class _ObserveOnlyContractExecution(_ContractExecution):
    """Mode contract whose single scope is OBSERVE_ONLY: the would-submit
    projection stays order_ready=false / SCOPE_OBSERVE_ONLY."""

    def n_leg_mode_contract(self) -> dict[str, object]:
        contract = super().n_leg_mode_contract()
        contract["execution_scopes"] = {
            "s1": {"scope_id": "s1", "capability": "OBSERVE_ONLY", "scope_version": 1},
        }
        return contract


class _TaxonomyHttpRuntime(_FakeRuntime):
    """Fence-2 runtime with one seeded taxonomy component and a real catalog
    whose current generation holds matching relations (#105)."""

    def __init__(self, *, legacy_retired: bool, catalog_dir, solutions) -> None:
        super().__init__(legacy_retired=legacy_retired)
        self.store = _RetiredStateStore()
        self.monitor = _RetiredStateMonitor()
        self.execution = _ObserveOnlyContractExecution()
        catalog = RelationCatalog(catalog_dir)
        for payload in (
            _implies_relation_discovery("cond-a", "cond-b"),
            _implies_relation_discovery("cat-a", "cat-b"),
            _native_complement_relation_discovery(),
        ):
            version_id = catalog.ingest(payload)["version_id"]
            catalog.approve(
                version_id, {"version_id": version_id}, actor="op", git_sha="sha"
            )
        self.relation_catalog = catalog
        self.n_leg_solutions = lambda: list(solutions)  # noqa: E731


def test_fence2_retired_rows_carry_taxonomy_episode_and_leg_display(tmp_path) -> None:
    solution = _taxonomy_component_solution(
        "component:cond-a:cond-b", ["cond-a", "cond-b"]
    )
    with _serve(
        _TaxonomyHttpRuntime(
            legacy_retired=True, catalog_dir=tmp_path, solutions=[solution]
        )
    ) as base:
        status, state = _get_state(base)

    assert status == 200
    opportunities = state["opportunities"]
    assert [row["component_id"] for row in opportunities] == [
        "component:cond-a:cond-b"
    ]
    row = opportunities[0]
    assert row["opportunity_id"] == "nleg:component:cond-a:cond-b"
    assert row["engine_owner"] == "N_LEG"
    assert row["relation_type"] == "IMPLIES"
    assert row["discovery_source"] == "LLM"
    assert row["leg_count"] == 2
    assert row["scope"] == {"event": "same_event", "venue": "same_venue"}
    assert row["scope_label"] == "同所 · 同事件"
    assert row["order_ready"] is False
    assert row["reason"] == "SCOPE_OBSERVE_ONLY"
    assert row["partial_fill_proof"] == "PARTIAL_FILL_SAFE"
    assert row["episode"] == {
        "opportunity_episode_id": None,
        "episode_lineage_id": None,
        "status": None,
        "opened_at": None,
        "duration_seconds": None,
        "would_submit_ready_seconds": None,
        "best_guaranteed_profit": None,
        "close_reason": None,
    }
    assert [leg["venue"] for leg in row["legs"]] == ["polymarket", "polymarket"]
    assert [leg["expires_at"] for leg in row["legs"]] == [
        "2026-12-31",
        "2026-12-31",
    ]


def test_fence2_duplicate_component_projections_surface_one_row(tmp_path) -> None:
    solution = _taxonomy_component_solution(
        "component:cond-a:cond-b", ["cond-a", "cond-b"]
    )
    with _serve(
        _TaxonomyHttpRuntime(
            legacy_retired=True,
            catalog_dir=tmp_path,
            solutions=[solution, dict(solution)],
        )
    ) as base:
        status, state = _get_state(base)

    assert status == 200
    assert len(state["opportunities"]) == 1
    assert state["opportunities"][0]["engine_owner"] == "N_LEG"


def test_fence2_merged_component_joins_relation_taxonomy_sorted(tmp_path) -> None:
    solution = _taxonomy_component_solution(
        "component:cat-a:cat-b:cat-c", ["cat-a", "cat-b", "cat-c"]
    )
    with _serve(
        _TaxonomyHttpRuntime(
            legacy_retired=True, catalog_dir=tmp_path, solutions=[solution]
        )
    ) as base:
        status, state = _get_state(base)

    assert status == 200
    row = state["opportunities"][0]
    assert row["relation_type"] == "IMPLIES/NATIVE_COMPLEMENT"
    assert row["discovery_source"] == "LLM/VENUE_METADATA"
    assert row["leg_count"] == 3


def test_fence2_component_without_catalog_match_keeps_empty_taxonomy(tmp_path) -> None:
    solution = _taxonomy_component_solution(
        "component:unmatched-1:unmatched-2", ["unmatched-1", "unmatched-2"]
    )
    with _serve(
        _TaxonomyHttpRuntime(
            legacy_retired=True, catalog_dir=tmp_path, solutions=[solution]
        )
    ) as base:
        status, state = _get_state(base)

    assert status == 200
    opportunities = state["opportunities"]
    assert [row["component_id"] for row in opportunities] == [
        "component:unmatched-1:unmatched-2"
    ]
    row = opportunities[0]
    assert row["engine_owner"] == "N_LEG"
    assert row["relation_type"] is None
    assert row["discovery_source"] is None
    assert row["scope"] is None
    assert row["scope_label"] is None
    assert row["episode"] == {
        "opportunity_episode_id": None,
        "episode_lineage_id": None,
        "status": None,
        "opened_at": None,
        "duration_seconds": None,
        "would_submit_ready_seconds": None,
        "best_guaranteed_profit": None,
        "close_reason": None,
    }
    for leg in row["legs"]:
        assert leg["venue"] is None
        assert leg["expires_at"] is None


def _seeded_n_leg_solution() -> dict[str, object]:
    market = canonical_payload(
        MarketSolution(
            component_id="c1",
            structure_fingerprint="sha256:struct",
            quote_fingerprint="sha256:quote",
            quantities=(ActionQuantity("a-yes", 20), ActionQuantity("a-no", 20)),
            guaranteed_profit_units=8_400_000,
            bounded_cost_units=31_200_000,
            bounded_payout_units=39_600_000,
            capital_release_at=datetime(2026, 8, 30, tzinfo=UTC),
            global_search_closed=False,
            verification_fingerprint="sha256:verify",
        )
    )
    return {
        "component_id": "c1",
        "scope_id": "s1",
        "market": market,
        "execution": {
            "market_solution_fingerprint": fingerprint(canonical_payload(market)),
            "quantities": market["quantities"],
            "capital_use_units": 31_200_000,
            "reason": "EXECUTABLE",
            "order_ready": False,
            "partial_fill_proof": "PARTIAL_FILL_SAFE",
        },
    }


class _RetiredHttpRuntime(_FakeRuntime):
    """Production fence runtime whose monitor still carries a legacy row and
    whose N_LEG pipeline has one seeded solution (the /state seam)."""

    def __init__(self, *, legacy_retired: bool) -> None:
        super().__init__(legacy_retired=legacy_retired)
        self.store = _RetiredStateStore()
        self.monitor = _RetiredStateMonitor()
        self.execution = _ContractExecution()
        self.n_leg_solutions = lambda: [_seeded_n_leg_solution()]  # noqa: E731


def _get_state(base: str) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        base + "/api/prediction-arbitrage/state",
        headers={"Cookie": "ot_prediction_session=session-token"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_fence2_state_endpoint_serves_n_leg_owned_opportunities() -> None:
    with _serve(_RetiredHttpRuntime(legacy_retired=True)) as base:
        status, state = _get_state(base)

    assert status == 200
    opportunities = state["opportunities"]
    assert opportunities, "the seeded N_LEG solution must surface on /state"
    assert all(row["engine_owner"] == "N_LEG" for row in opportunities)
    assert not any(row["engine_owner"] == "yes_no" for row in opportunities)
    assert [row["component_id"] for row in opportunities] == ["c1"]


def test_fence1_state_endpoint_keeps_legacy_monitor_rows() -> None:
    with _serve(_RetiredHttpRuntime(legacy_retired=False)) as base:
        status, state = _get_state(base)

    assert status == 200
    opportunities = state["opportunities"]
    assert [
        row["opportunity_id"] for row in opportunities
    ] == ["legacy-1"], opportunities
    assert all(row["engine_owner"] != "N_LEG" for row in opportunities)


def test_fence2_runtime_runs_without_wiring_the_legacy_auto_eat_observer(
    tmp_path, monkeypatch
) -> None:
    import types

    import open_trader.prediction_runtime as runtime_module

    events: list[str] = []
    monkeypatch.setattr(
        runtime_module,
        "read_minimum_reader_generation",
        lambda _data_dir: events.append("fence.read") or 2,
        raising=False,
    )

    class FakeStore:
        def __init__(self, _data_dir) -> None:
            pass

        def apply_safety_policy(self, _policy, *, git_sha):
            return {"state": "baseline_enrolled"}

        def n_leg_scope(self, _scope_id):
            return None

        def close(self) -> None:
            pass

    class FakeTrading:
        def close(self) -> None:
            pass

    class FakeTradingClient:
        @classmethod
        def from_keychain(cls, _config):
            return FakeTrading()

    class FakeMonitor:
        def __init__(self, **_) -> None:
            pass

        def set_ready_observer(self, _observer) -> None:
            pass

        def set_observation_observer(self, _observer) -> None:
            pass

        def set_auto_eat_observer(self, _observer) -> None:
            events.append("auto_eat.bind")

        def set_failure_observer(self, _observer) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeExecution:
        def __init__(self, **_) -> None:
            pass

        def reconcile_startup(self):
            return {"status": "ready"}

        def notify_ready_opportunity(self, *_, **__):
            pass

        def notify_observation(self, *_, **__):
            pass

        def notify_monitor_failure(self, *_, **__):
            pass

        def auto_eat_threshold(self, *_, **__):
            pass

        def set_cross_venue_monitor(self, _monitor) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        runtime_module,
        "load_trading_config",
        lambda _path: types.SimpleNamespace(
            signer_address="0x1111111111111111111111111111111111111111",
            wallet_address="0x2222222222222222222222222222222222222222",
            predict=None,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        runtime_module, "PolymarketTradingClient", FakeTradingClient, raising=False
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        types.SimpleNamespace(from_keychain=lambda _config: None),
        raising=False,
    )
    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", FakeStore, raising=False)
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor, raising=False)
    monkeypatch.setattr(
        runtime_module, "PredictionExecutionService", FakeExecution, raising=False
    )
    monkeypatch.setattr(
        runtime_module, "LlmRelationValidator", lambda *a, **k: object(), raising=False
    )
    monkeypatch.setattr(
        runtime_module, "LlmTitleTranslator", lambda *a, **k: object(), raising=False
    )

    runtime = runtime_module.PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        cross_venue_monitor=runtime_module._UnavailableCrossVenueMonitor("fence2-test"),
        git_sha="sha-1",
        reader_generation=2,
        solver_server_factory=lambda: object(),
        enable_n_leg_background=False,
    )
    runtime.start()
    try:
        assert runtime.state == "RUNNING"
        assert runtime.legacy_retired is True
        assert "auto_eat.bind" not in events
        assert events.count("fence.read") == 1
    finally:
        runtime.stop()


def _observer_wiring_runtime(
    monkeypatch: object, tmp_path: object, *, fence: int
) -> tuple[object, list[str]]:
    """Shared harness of the #109 observer-wiring tests: a production
    PredictionRuntime whose reader fence is `fence`, built exactly like the
    auto-eat unwiring test above, whose monitor records every
    ``set_*_observer`` call as ``<name>.bind``."""

    import types

    import open_trader.prediction_runtime as runtime_module

    events: list[str] = []
    monkeypatch.setattr(
        runtime_module,
        "read_minimum_reader_generation",
        lambda _data_dir: events.append("fence.read") or fence,
        raising=False,
    )

    class FakeStore:
        def __init__(self, _data_dir) -> None:
            pass

        def apply_safety_policy(self, _policy, *, git_sha):
            return {"state": "baseline_enrolled"}

        def n_leg_scope(self, _scope_id):
            return None

        def close(self) -> None:
            pass

    class FakeTrading:
        def close(self) -> None:
            pass

    class FakeTradingClient:
        @classmethod
        def from_keychain(cls, _config):
            return FakeTrading()

    class FakeMonitor:
        def __init__(self, **_) -> None:
            pass

        def set_ready_observer(self, _observer) -> None:
            events.append("ready.bind")

        def set_observation_observer(self, _observer) -> None:
            events.append("observation.bind")

        def set_auto_eat_observer(self, _observer) -> None:
            events.append("auto_eat.bind")

        def set_failure_observer(self, _observer) -> None:
            events.append("failure.bind")

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeExecution:
        def __init__(self, **_) -> None:
            pass

        def reconcile_startup(self):
            return {"status": "ready"}

        def notify_ready_opportunity(self, *_, **__):
            pass

        def notify_observation(self, *_, **__):
            pass

        def notify_monitor_failure(self, *_, **__):
            pass

        def auto_eat_threshold(self, *_, **__):
            pass

        def set_cross_venue_monitor(self, _monitor) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        runtime_module,
        "load_trading_config",
        lambda _path: types.SimpleNamespace(
            signer_address="0x1111111111111111111111111111111111111111",
            wallet_address="0x2222222222222222222222222222222222222222",
            predict=None,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        runtime_module, "PolymarketTradingClient", FakeTradingClient, raising=False
    )
    monkeypatch.setattr(
        runtime_module,
        "PredictTradingClient",
        types.SimpleNamespace(from_keychain=lambda _config: None),
        raising=False,
    )
    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", FakeStore, raising=False)
    monkeypatch.setattr(runtime_module, "PolymarketMonitor", FakeMonitor, raising=False)
    monkeypatch.setattr(
        runtime_module, "PredictionExecutionService", FakeExecution, raising=False
    )
    monkeypatch.setattr(
        runtime_module, "LlmRelationValidator", lambda *a, **k: object(), raising=False
    )
    monkeypatch.setattr(
        runtime_module, "LlmTitleTranslator", lambda *a, **k: object(), raising=False
    )

    runtime = runtime_module.PredictionRuntime(
        data_dir=tmp_path,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8766/",
        cross_venue_monitor=runtime_module._UnavailableCrossVenueMonitor(
            "observer-wiring-test"
        ),
        git_sha="sha-1",
        reader_generation=fence,
        solver_server_factory=lambda: object(),
        enable_n_leg_background=False,
    )
    return runtime, events


def test_fence2_runtime_runs_without_wiring_the_legacy_notification_observers(
    tmp_path, monkeypatch
) -> None:
    """Issue #109: at the N_LEG fence the legacy ready/observation alert
    channels must be silent — no Feishu cards, no legacy opportunity alerts.
    Mirrors the auto-eat unwiring test above: the production wiring must not
    arm either observer once legacy_retired is true, while the failure
    observer keeps its unconditional wiring."""

    runtime, events = _observer_wiring_runtime(
        monkeypatch, tmp_path, fence=2
    )
    runtime.start()
    try:
        assert runtime.state == "RUNNING"
        assert runtime.legacy_retired is True
        assert "ready.bind" not in events
        assert "observation.bind" not in events
        assert "auto_eat.bind" not in events
        assert "failure.bind" in events
        assert events.count("fence.read") == 1
    finally:
        runtime.stop()


def test_fence1_runtime_still_wires_all_three_legacy_observers(
    tmp_path, monkeypatch
) -> None:
    """Issue #109 fence-1 pin: below the N_LEG fence the ready, observation,
    and auto-eat observers keep their exact pre-#109 wiring (one bind each),
    alongside the unconditional failure observer."""

    runtime, events = _observer_wiring_runtime(
        monkeypatch, tmp_path, fence=1
    )
    runtime.start()
    try:
        assert runtime.state == "RUNNING"
        assert runtime.legacy_retired is False
        assert events.count("ready.bind") == 1
        assert events.count("observation.bind") == 1
        assert events.count("auto_eat.bind") == 1
        assert events.count("failure.bind") == 1
    finally:
        runtime.stop()


def test_fence2_cross_auto_is_observe_only_despite_armed_auto_submit(tmp_path) -> None:
    # Fixture pattern of tests/test_prediction_arbitrage_execution.py
    # (_cross_service / test_auto_submit_cross_venue_runs_once_...): a real
    # store plus the module's cross fakes; the only difference is the fence.
    from open_trader.prediction_arbitrage_execution import PredictionExecutionService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from tests.test_prediction_arbitrage_execution import (
        ChannelNotifier,
        CompositeTestNotifier,
        CrossPolymarketTrading,
        CrossPredictTrading,
        CrossVenueMonitor,
        FakeMonitor,
        _cross_intent,
        _cross_venue_notification_signal,
        _intent,
    )

    store = PredictionArbitrageStore(tmp_path / "data")
    trading = CrossPolymarketTrading()
    cross = CrossVenueMonitor(_cross_intent())
    predict = CrossPredictTrading()
    service = PredictionExecutionService(
        store=store,
        monitor=FakeMonitor(_intent()),
        trading=trading,
        predict_trading=predict,
        notifier=CompositeTestNotifier(
            ChannelNotifier("macos"), ChannelNotifier("feishu")
        ),
        lock_path=tmp_path / "execution.lock",
        legacy_retired=True,
    )
    service.set_cross_venue_monitor(cross)
    assert service.reconcile_startup()["state"] == "ready"

    store.set_cross_auto_mode("auto_submit", "operator_configured")
    assert store.arm_cross_auto()["armed"] is True

    status = service.cross_auto_status()
    assert status["configured_mode"] == "auto_submit"
    assert status["armed"] is True
    assert status["effective_mode"] == "observe_only"

    signal_id = _cross_venue_notification_signal(store)
    store.update_signal(signal_id, {"execution_mode": "auto_submit"})
    service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )

    assert trading.cross_submit_calls == 0
    assert predict.submit_calls == 0
    assert store.cross_auto_attempts() == []


def test_fence2_data_dir_rejects_reader_generation_1_release_manifest(
    tmp_path, monkeypatch
) -> None:
    import open_trader.prediction_runtime as runtime_module
    from open_trader.prediction_arbitrage_store import (
        N_LEG_READER_GENERATION,
        PredictionArbitrageStore,
        read_minimum_reader_generation,
    )
    from open_trader.prediction_release import load_prediction_release_manifest
    from open_trader.prediction_runtime import (
        PredictionRuntime,
        PredictionRuntimeCompatibilityError,
    )

    # A real data dir whose reader fence has been advanced to the N_LEG
    # generation, plus an old release manifest still pinned at generation 1.
    data_dir = tmp_path / "data"
    seed_store = PredictionArbitrageStore(data_dir)
    assert (
        seed_store.advance_minimum_reader_generation(N_LEG_READER_GENERATION) == 2
    )
    assert read_minimum_reader_generation(data_dir) == 2
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "open_trader.prediction_service.release.v1",
                "reader_generation": 1,
                "contract_generation": 1,
            }
        ),
        encoding="utf-8",
    )
    manifest = load_prediction_release_manifest(manifest_path)

    writes: list[str] = []

    def _forbidden_store(_data_dir):
        writes.append("store")
        raise AssertionError("production write store must not be constructed")

    monkeypatch.setattr(runtime_module, "PredictionArbitrageStore", _forbidden_store)
    monkeypatch.setattr(
        runtime_module.PolymarketTradingClient,
        "from_keychain",
        lambda _config: writes.append("trading") or object(),
    )

    runtime = PredictionRuntime(
        data_dir=data_dir,
        prediction_config_path=tmp_path / "prediction.json",
        dashboard_url="http://127.0.0.1:8769",
        reader_generation=manifest.reader_generation,
    )

    with pytest.raises(
        PredictionRuntimeCompatibilityError,
        match="reader generation 1 is below required 2",
    ):
        runtime.start()

    assert runtime.state == "FAILED"
    assert runtime.state != "RUNNING"
    assert runtime.production_owner is False
    assert writes == []
    probe = runtime_module._RuntimeOwnershipLock(
        tmp_path / "prediction_arbitrage" / "runtime.lock"
    )
    probe.acquire()
    probe.release()


def test_fence2_history_still_returns_legacy_rows_with_original_strategy_type() -> None:
    legacy_signal_row = {
        "signal_id": "signal-legacy-1",
        "opportunity_id": "same-venue-1",
        "market_id": "market-1",
        "market_type": "standard_binary",
        "strategy_type": "yes_no",
        "started_at": "2026-08-10T01:02:03Z",
        "question": "Legacy yes/no signal",
    }

    class HistoryStore(_Store):
        def signal_history(self, _window: str) -> list[dict[str, object]]:
            return [legacy_signal_row]

    runtime = _FakeRuntime(legacy_retired=True)
    runtime.store = HistoryStore()
    with _serve(runtime) as base:
        request = urllib.request.Request(
            base
            + "/api/prediction-arbitrage/history?kind=signals&limit=20&offset=0",
            headers={"Cookie": "ot_prediction_session=session-token"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))

    assert status == 200
    assert payload["total"] == 1
    assert payload["items"][0]["strategy_type"] == "yes_no"
    assert payload["items"][0]["signal_id"] == "signal-legacy-1"


def test_dashboard_hides_legacy_controls_when_contract_generation_is_2() -> None:
    from tests.test_dashboard_web import run_dashboard_js

    output = run_dashboard_js(r'''
const payload = {
  n_leg: {contract_generation: 2, mode: "MANUAL"},
  cross_auto: {configured_mode: "auto_submit", armed: true, effective_mode: "auto_submit"},
};
const card = predictionUnifiedOpportunityCard(
  {
    opportunity_id: "nleg:c1",
    component_id: "c1",
    engine_owner: "N_LEG",
    qualification: {status: "QUALIFIED_VERIFIED", checks: [], order_ready: true},
  },
  "MANUAL",
  predictionLegacyControlsRetired(payload),
);
console.log(JSON.stringify({
  modeBar: predictionModeBar(payload),
  crossAuto: predictionCrossAutoStatus(payload),
  card,
}));
''')
    rendered = json.loads(output)
    assert "data-action='set-mode'" not in rendered["modeBar"].replace('"', "'")
    assert "pm-mode-button" not in rendered["modeBar"]
    assert rendered["crossAuto"] == ""
    assert "data-action=\"participate\"" not in rendered["card"]
    assert "pm-participate" not in rendered["card"]


def test_dashboard_keeps_legacy_controls_when_contract_generation_is_1() -> None:
    from tests.test_dashboard_web import run_dashboard_js

    output = run_dashboard_js(r'''
const payload = {
  n_leg: {contract_generation: 1, mode: "MANUAL"},
  cross_auto: {configured_mode: "auto_submit", armed: true, effective_mode: "auto_submit"},
};
const card = predictionUnifiedOpportunityCard(
  {
    opportunity_id: "opp-1",
    engine_owner: "yes_no",
    qualification: {status: "QUALIFIED_VERIFIED", checks: [], order_ready: true},
  },
  "MANUAL",
  predictionLegacyControlsRetired(payload),
);
console.log(JSON.stringify({
  modeBar: predictionModeBar(payload),
  crossAuto: predictionCrossAutoStatus(payload),
  card,
}));
''')
    rendered = json.loads(output)
    assert "pm-mode-button" in rendered["modeBar"]
    assert "pause-cross-auto" in rendered["crossAuto"]
    assert "data-action=\"participate\"" in rendered["card"]


def test_dashboard_retired_n_leg_card_renders_only_the_n_leg_execution_plan() -> None:
    from tests.test_dashboard_web import run_dashboard_js

    output = run_dashboard_js(r'''
const leg = (actionId, outcome) => ({
  action_id: actionId,
  quantity_lots: 20,
  max_price: "0.480",
  max_cost: "9600000",
  venue: "polymarket",
  outcome,
  settlement_asset: "pUSD",
});
const solution = {
  component_id: "c1",
  market: {legs: [leg("a-yes", "YES"), leg("a-no", "NO")]},
  execution: {
    order_ready: false,
    reason: "EXECUTABLE",
    legs: [leg("a-yes", "YES"), leg("a-no", "NO")],
  },
};
// the retired-mode row shape: the projection market/execution display fields
// are spread flat onto the row and the full projection rides n_leg_solution.
const row = {
  opportunity_id: "nleg:c1",
  component_id: "c1",
  market_type: "n_leg",
  strategy_type: "N_LEG",
  engine_owner: "N_LEG",
  leg_count: 2,
  ...solution.market,
  ...solution.execution,
  qualification: {status: "QUALIFIED_VERIFIED", checks: [], order_ready: false},
  n_leg_solution: solution,
};
const payload = {n_leg: {contract_generation: 2, mode: "MANUAL"}};
console.log(JSON.stringify(predictionUnifiedOpportunityCard(
  row, "MANUAL", predictionLegacyControlsRetired(payload)
)));
''')
    card = json.loads(output)
    assert "pm-execution-plan" in card
    assert "下单计划" in card
    assert "20 份 · 最高 $0.480" in card
    # the generic placeholder leg block (empty quantity "- 份") must not render
    # above the N_LEG plan; the plan's two real legs are the only leg rows.
    assert "- 份 · 最高 -" not in card
    assert card.count('class="pm-order-leg"') == 2


def test_dashboard_generation1_legacy_card_keeps_its_normal_legs() -> None:
    from tests.test_dashboard_web import run_dashboard_js

    output = run_dashboard_js(r'''
const payload = {n_leg: {contract_generation: 1, mode: "MANUAL"}};
const row = {
  opportunity_id: "opp-1",
  engine_owner: "yes_no",
  market_type: "standard_binary",
  quantity: "20",
  yes_price: "0.480",
  no_price: "0.480",
  yes_cost: "9.60",
  no_cost: "9.60",
  legs: [{}, {}],
  qualification: {status: "QUALIFIED_VERIFIED", checks: [], order_ready: true},
};
console.log(JSON.stringify(predictionUnifiedOpportunityCard(
  row, "MANUAL", predictionLegacyControlsRetired(payload)
)));
''')
    card = json.loads(output)
    assert "第 1 腿 · Polymarket · BUY YES" in card
    assert "第 2 腿 · Polymarket · BUY NO" in card
    assert "20 份 · 最高 $0.480" in card
    assert "pm-execution-plan" not in card


def test_fence2_state_endpoint_carries_episode_projection(tmp_path) -> None:
    """#106: a runtime exposing n_leg_episodes fills the retired row's
    episode slot through the HTTP state endpoint."""
    solution = _taxonomy_component_solution(
        "component:cond-a:cond-b", ["cond-a", "cond-b"]
    )
    episode_projection = {
        "component:cond-a:cond-b": {
            "opportunity_episode_id": "e" * 32,
            "episode_lineage_id": "lineage-1",
            "status": "ONGOING",
            "opened_at": "2026-09-01T09:23:00+00:00",
            "duration_seconds": 2220,
            "would_submit_ready_seconds": 720,
            "best_guaranteed_profit": "9.60",
            "close_reason": None,
        }
    }

    class EpisodeRuntime(_TaxonomyHttpRuntime):
        def __init__(self, *, legacy_retired: bool, catalog_dir, solutions) -> None:
            super().__init__(
                legacy_retired=legacy_retired,
                catalog_dir=catalog_dir,
                solutions=solutions,
            )
            self.n_leg_episodes = lambda: dict(episode_projection)  # noqa: E731

    with _serve(
        EpisodeRuntime(
            legacy_retired=True, catalog_dir=tmp_path, solutions=[solution]
        )
    ) as base:
        status, state = _get_state(base)

    assert status == 200
    row = state["opportunities"][0]
    episode = row["episode"]
    assert episode["opportunity_episode_id"] == "e" * 32
    assert episode["episode_lineage_id"] == "lineage-1"
    assert episode["status"] == "ONGOING"
    assert episode["opened_at"] == "2026-09-01T09:23:00+00:00"
    assert episode["duration_seconds"] == 2220
    assert episode["would_submit_ready_seconds"] == 720
    assert episode["best_guaranteed_profit"] == "9.60"
    assert episode["close_reason"] is None
