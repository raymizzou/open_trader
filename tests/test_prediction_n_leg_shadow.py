from __future__ import annotations

from concurrent.futures import Future
from datetime import UTC, datetime
from decimal import Decimal

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from types import SimpleNamespace

import pytest

from open_trader.prediction_n_leg import (
    ActionQuantity,
    PortfolioCandidate,
    canonical_payload,
    fingerprint,
)
from open_trader.prediction_n_leg_oracle import (
    cut_from_scenario,
    evaluate_fixed_portfolio,
)

from open_trader.prediction_n_leg_shadow import (
    NLegShadowClient,
    NLegShadowScheduler,
    legacy_shadow_snapshot,
    legacy_shadow_request,
)
from open_trader.prediction_solver import ObjectiveBounds, SolverEvidence
from open_trader.prediction_solver_verified import (
    CANDIDATE_EVIDENCE_SCHEMA_V1,
    PROOF_REQUEST_SCHEMA_V1,
    CandidateEvidence,
    ProofInput,
    model_fingerprint,
    quote_fingerprint,
)


def _signal(store: PredictionArbitrageStore) -> str:
    return store.upsert_signal(
        {
            "market_id": "market-1",
            "market_type": "standard_binary",
            "started_at": "2026-08-15T00:00:00Z",
        }
    )


def test_shadow_scheduler_is_nonblocking_dedupes_and_keeps_the_latest_result(
    tmp_path,
) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    signal_id = _signal(store)
    pending: list[Future[dict[str, object]]] = []

    def submit_snapshot(_snapshot: dict[str, object]) -> Future[dict[str, object]]:
        result: Future[dict[str, object]] = Future()
        pending.append(result)
        return result

    def result(fingerprint: str) -> dict[str, object]:
        return {
            "run_status": "SUCCESS",
            "decision": "QUALIFIED_VERIFIED",
            "comparison": "CONSISTENT",
            "fingerprint": fingerprint,
            "result": {"minimum_profit": "1.00"},
        }

    scheduler = NLegShadowScheduler(store, submit_snapshot=submit_snapshot)
    try:
        assert scheduler.schedule(signal_id, {"fingerprint": "first"}) == "scheduled"
        assert scheduler.schedule(signal_id, {"fingerprint": "first"}) == "deduped"
        assert scheduler.schedule(signal_id, {"fingerprint": "second"}) == "scheduled"
        assert len(pending) == 1
        pending.pop(0).set_result(result("first"))
        assert len(pending) == 1
        pending.pop(0).set_result(result("second"))
        assert scheduler.wait_idle(2)
    finally:
        scheduler.close()

    shadow = store.signal(signal_id)["n_leg_shadow"]
    assert shadow["latest_fingerprint"] == "second"
    assert shadow["latest_result"]["fingerprint"] == "second"
    assert shadow["run_count"] == 2
    assert shadow["qualified_count"] == 2
    assert shadow["difference_count"] == 0
    assert shadow["failure_count"] == 0


def test_legacy_yes_no_adapter_builds_a_canonical_worker_request() -> None:
    request = legacy_shadow_request(
        {
            "opportunity_id": "event-1:market-1",
            "market_id": "market-1",
            "market_type": "standard_binary",
            "quantity": "10",
            "yes_max_cost": "4.20",
            "no_max_cost": "4.70",
            "total_max_cost": "8.90",
            "minimum_profit": "1.10",
            "confirmed_at": "2026-08-15T00:00:00Z",
            "resolution_at": "2026-08-16T00:00:00Z",
            "signal_id": "episode-1",
            "fingerprint": "shadow-1",
        }
    )

    assert request.backend == "cp_sat"
    assert request.request_id == "shadow:episode-1:shadow-1"
    assert tuple(action.quantity_scale for action in request.request.problem.actions) == (1, 1)
    assert tuple(action.max_quantity_lots for action in request.request.problem.actions) == (10, 10)


def test_legacy_adapter_missing_settlement_evidence_returns_diagnostic() -> None:
    result = legacy_shadow_request(
        {
            "market_id": "market-1",
            "market_type": "standard_binary",
            "quantity": "10",
            "yes_max_cost": "4.20",
            "no_max_cost": "4.70",
            "minimum_profit": "1.10",
            "confirmed_at": "2026-08-15T00:00:00Z",
            "fingerprint": "shadow-1",
        }
    )

    assert result["run_status"] == "SUCCESS"
    assert result["decision"] == "UNKNOWN"
    assert result["comparison"] == "DIFFERENCE"
    assert result["differences"]["capital_release_at"]["reason"] == "缺少结算证据"


def test_legacy_snapshot_is_canonical_and_changes_with_real_economics() -> None:
    source = {
        "opportunity_id": "event-1:market-1",
        "market_id": "market-1",
        "market_type": "standard_binary",
        "quantity": "10",
        "yes_max_cost": "4.20",
        "no_max_cost": "4.70",
        "total_max_cost": "8.90",
        "minimum_profit": "1.10",
        "confirmed_at": "2026-08-15T00:00:00Z",
        "resolution_at": "2026-08-16T00:00:00Z",
    }

    snapshot = legacy_shadow_snapshot(source, "episode-1")
    changed = legacy_shadow_snapshot({**source, "yes_max_cost": "4.30"}, "episode-1")

    assert snapshot["signal_id"] == "episode-1"
    assert snapshot["total_max_cost"] == "8.90"
    assert snapshot["fingerprint"] != changed["fingerprint"]


def test_legacy_snapshot_fingerprint_ignores_observation_timestamps() -> None:
    source = {
        "opportunity_id": "event-1:market-1",
        "market_id": "market-1",
        "market_type": "standard_binary",
        "quantity": "10",
        "yes_max_cost": "4.20",
        "no_max_cost": "4.70",
        "total_max_cost": "8.90",
        "minimum_profit": "1.10",
        "confirmed_at": "2026-08-15T00:00:00Z",
        "resolution_at": "2026-08-16T00:00:00Z",
    }

    snapshot = legacy_shadow_snapshot(source, "episode-1")
    relived = legacy_shadow_snapshot(
        {
            **source,
            "confirmed_at": "2026-08-15T00:00:01Z",
        },
        "episode-1",
    )

    assert snapshot["confirmed_at"] != relived["confirmed_at"]
    assert snapshot["fingerprint"] == relived["fingerprint"]


def test_shadow_scheduler_dedupes_identical_economics_with_different_timestamps(
    tmp_path,
) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    signal_id = _signal(store)
    pending: list[Future[dict[str, object]]] = []
    scheduler = NLegShadowScheduler(
        store,
        submit_snapshot=lambda _snapshot: (
            pending.append(Future()) or pending[-1]
        ),
    )
    source = {
        "opportunity_id": "event-1:market-1",
        "market_id": "market-1",
        "market_type": "standard_binary",
        "quantity": "10",
        "yes_max_cost": "4.20",
        "no_max_cost": "4.70",
        "total_max_cost": "8.90",
        "minimum_profit": "1.10",
        "confirmed_at": "2026-08-15T00:00:00Z",
        "resolution_at": "2026-08-16T00:00:00Z",
    }
    try:
        assert scheduler.schedule(
            signal_id, legacy_shadow_snapshot(source, "episode-1")
        ) == "scheduled"
        assert scheduler.schedule(
            signal_id,
            legacy_shadow_snapshot(
                {**source, "confirmed_at": "2026-08-15T00:05:00Z"},
                "episode-1",
            ),
        ) == "deduped"
        assert len(pending) == 1
    finally:
        scheduler.close()


def test_shadow_client_missing_settlement_evidence_is_a_diagnostic_not_failure() -> None:
    class Server:
        def submit(self, _request):
            raise AssertionError("no worker request may be dispatched")

    result = NLegShadowClient(Server()).submit(
        {
            "opportunity_id": "event-1:market-1",
            "market_id": "market-1",
            "market_type": "standard_binary",
            "quantity": "10",
            "yes_max_cost": "4.20",
            "no_max_cost": "4.70",
            "total_max_cost": "8.90",
            "minimum_profit": "1.10",
            "confirmed_at": "2026-08-15T00:00:00Z",
            "signal_id": "episode-1",
            "fingerprint": "shadow-1",
        }
    ).result()

    assert result["run_status"] == "SUCCESS"
    assert result["decision"] == "UNKNOWN"
    assert result["comparison"] == "DIFFERENCE"
    assert result["differences"]["capital_release_at"]["reason"] == "缺少结算证据"


def test_shadow_summary_replaces_queued_snapshot_and_keeps_normal_unknown_nonfatal(
    tmp_path,
) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    signal_id = _signal(store)
    pending: list[Future[dict[str, object]]] = []

    def submit(_snapshot: dict[str, object]) -> Future[dict[str, object]]:
        future: Future[dict[str, object]] = Future()
        pending.append(future)
        return future

    scheduler = NLegShadowScheduler(store, submit_snapshot=submit)
    try:
        scheduler.schedule(signal_id, {"fingerprint": "first"})
        scheduler.schedule(signal_id, {"fingerprint": "second"})
        scheduler.schedule(signal_id, {"fingerprint": "third"})
        pending.pop(0).set_result(
            {
                "run_status": "SUCCESS", "decision": "UNKNOWN", "comparison": "DIFFERENCE",
                "fingerprint": "first", "differences": {"minimum_profit": {"absolute": "1"}},
            }
        )
        assert len(pending) == 1
        pending.pop(0).set_result(
            {
                "run_status": "SUCCESS", "decision": "NOT_QUALIFIED", "comparison": "DIFFERENCE",
                "fingerprint": "third", "differences": {"minimum_profit": {"absolute": "2"}},
            }
        )
        assert scheduler.wait_idle(2)
        assert scheduler.schedule(signal_id, {"fingerprint": "third"}) == "deduped"
    finally:
        scheduler.close()

    summary = store.signal(signal_id)["n_leg_shadow"]
    assert summary["run_count"] == 2
    assert summary["failure_count"] == 0
    assert summary["latest_result"]["fingerprint"] == "third"
    assert summary["current_differences"] == {"minimum_profit": {"absolute": "2"}}
    assert summary["max_differences"] == {"minimum_profit": {"absolute": "2"}}


def test_shadow_client_fails_closed_when_worker_cleanup_is_unproven() -> None:
    class Server:
        def submit(self, _request):
            result = Future()
            result.set_result(
                SimpleNamespace(
                    status="OK",
                    termination="COMPLETED",
                    cleanup_proven=False,
                    response=SimpleNamespace(evidence={"claimed": "qualified"}),
                )
            )
            return result

    result = NLegShadowClient(Server()).submit(
        {
            "opportunity_id": "event-1:market-1",
            "market_id": "market-1",
            "market_type": "standard_binary",
            "quantity": "10",
            "yes_max_cost": "4.20",
            "no_max_cost": "4.70",
            "minimum_profit": "1.10",
            "confirmed_at": "2026-08-15T00:00:00Z",
            "resolution_at": "2026-08-16T00:00:00Z",
            "fingerprint": "shadow-1",
        }
    ).result()

    assert result["run_status"] == "FAILURE"
    assert result["reason"] == "CLEANUP_UNPROVEN"


def test_shadow_client_compares_verified_economics_in_exact_units() -> None:
    snapshot = legacy_shadow_snapshot(
        {
            "opportunity_id": "event-1:market-1",
            "market_id": "market-1",
            "market_type": "standard_binary",
            "quantity": "10",
            "yes_max_cost": "4.20",
            "no_max_cost": "4.70",
            "total_max_cost": "8.90",
            "minimum_profit": "1.20",
            "confirmed_at": "2026-08-15T00:00:00Z",
            "resolution_at": "2026-08-16T00:00:00Z",
        },
        "episode-1",
    )
    request = legacy_shadow_request(snapshot)
    proof_input = ProofInput(
        PROOF_REQUEST_SCHEMA_V1,
        request.request,
        request.limits,
        quote_fingerprint(request.request.problem),
        0,
        "issue-54-test",
    )
    quantities = tuple(
        ActionQuantity(action.action_id, action.max_quantity_lots)
        for action in request.request.problem.actions
    )
    evaluation = evaluate_fixed_portfolio(
        request.request.problem, quantities, request.request.budget
    )
    evidence = CandidateEvidence(
        CANDIDATE_EVIDENCE_SCHEMA_V1,
        proof_input,
        "cp_sat",
        "test",
        model_fingerprint(request.request.problem),
        fingerprint({"quantities": quantities}),
        SolverEvidence(
            "FEASIBLE",
            PortfolioCandidate(quantities, evaluation.guaranteed_profit_units),
            ObjectiveBounds(evaluation.guaranteed_profit_units, None, None, False),
            evaluation.worst_scenario,
            evaluation.payout_lower_bound_units,
            evaluation.cost_upper_bound_units,
            evaluation.guaranteed_profit_units,
            evaluation.conservative_capital_release_at,
            True,
            False,
            1,
            1,
            (cut_from_scenario(request.request.problem, evaluation.worst_scenario),),
            None,
        ),
    )

    class Server:
        def submit(self, _request):
            future = Future()
            future.set_result(
                SimpleNamespace(
                    status="OK",
                    termination="COMPLETED",
                    cleanup_proven=True,
                    response=SimpleNamespace(evidence=canonical_payload(evidence)),
                )
            )
            return future

    result = NLegShadowClient(Server()).submit(snapshot).result()

    assert result["run_status"] == "SUCCESS"
    assert result["decision"] == "QUALIFIED_VERIFIED"
    assert result["comparison"] == "DIFFERENCE"
    assert result["differences"]["minimum_profit"] == {
        "legacy": "1.20", "n_leg": "1.1", "absolute": "0.10"
    }
    for dimension in (
        "opportunity_exists",
        "direction",
        "worst_case",
        "net_margin_1pct",
        "annualized_15pct",
        "capital_release_30d",
        "order_ready",
        "rejection_reasons",
    ):
        assert dimension in result["differences"], dimension


def test_scheduler_close_discards_late_worker_completion(tmp_path) -> None:
    store = PredictionArbitrageStore(tmp_path / "data")
    signal_id = _signal(store)
    pending: Future[dict[str, object]] = Future()
    scheduler = NLegShadowScheduler(store, submit_snapshot=lambda _snapshot: pending)

    scheduler.schedule(signal_id, {"fingerprint": "first"})
    scheduler.close()

    summary = store.signal(signal_id)["n_leg_shadow"]
    assert summary["run_count"] == 0
    assert "latest_result" not in summary
