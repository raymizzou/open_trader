from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from open_trader.prediction_market_solution import MarketSolution
from open_trader.prediction_n_leg import ActionQuantity, canonical_payload, fingerprint
from open_trader.prediction_read_model import (
    prediction_history_payload,
    prediction_state_payload,
)


class _Store:
    def llm_usage_24h(self) -> dict[str, int]:
        return {"calls": 4, "successes": 3, "failures": 1, "cache_hits": 2}

    def active_execution(self) -> None:
        return None

    def unacknowledged_incident(self) -> None:
        return None

    def cross_unsettled_principal(self) -> Decimal:
        return Decimal("3.25")

    def signal_history(self, _window: str) -> list[dict[str, object]]:
        return self.histories("signals")

    def histories(self, kind: str) -> list[dict[str, object]]:
        rows = {
            "signals": [{
                "signal_id": "signal-1",
                "opportunity_id": "same-venue-1",
                "market_id": "market-1",
                "started_at": "2026-08-10T01:02:03Z",
                "question": "Same venue question",
                "wallet_address": "0x1111111111111111111111111111111111111111",
            }],
            "executions": [{
                "execution_id": "execution-1",
                "state": "complete",
                "updated_at": "2026-08-10T01:03:04Z",
                "question": "Same venue question",
                "actual_cost": "9.50",
                "wallet": "0x1111111111111111111111111111111111111111",
            }],
            "incidents": [{
                "incident_id": "incident-1",
                "state": "resolved",
                "created_at": "2026-08-10T01:04:05Z",
                "question": "Same venue question",
                "reason": "reconciled",
                "wallet": "0x1111111111111111111111111111111111111111",
            }],
        }
        return rows[kind]


class _Monitor:
    def snapshot(self) -> dict[str, object]:
        return {
            "status": "healthy",
            "health": {"status": "healthy", "degraded_reasons": []},
            "heartbeat_at": "2026-08-10T01:00:00Z",
            "stale": False,
            "readiness": {
                "status": "ready",
                "geoblock": "allowed",
                "relayer": "ready",
                "wallet_address": "0x1111111111111111111111111111111111111111",
                "p_usd_balance": "12.50",
            },
            "events": [{
                "event_id": "event-1",
                "title": "Same venue question",
                "markets": [{
                    "market_id": "market-1",
                    "yes_token_id": "yes-1",
                    "no_token_id": "no-1",
                }],
            }],
            "opportunities": [{
                "opportunity_id": "same-venue-1",
                "market_type": "standard_binary",
                "market_id": "market-1",
                "question": "Same venue question",
                "actionable": True,
                "profit": "1.00",
            }],
        }


class _PredictSource:
    def snapshot(self) -> dict[str, object]:
        return {
            "rest": "ready",
            "ws": "ready",
            "wallet": "0x2222222222222222222222222222222222222222",
        }


class _CrossVenueMonitor:
    _predict = _PredictSource()

    def snapshot(self) -> dict[str, object]:
        return {
            "status": "ready",
            "mode": "observe_only",
            "events": [{
                "event_id": "cross-event-1",
                "market_type": "cross_venue_yes_no",
                "wallet": "0x2222222222222222222222222222222222222222",
            }],
            "opportunities": [{
                "opportunity_id": "cross-venue-1",
                "market_type": "cross_venue_yes_no",
                "question": "Cross venue question",
                "quantity": "5",
                "total_max_cost": "4.50",
                "minimum_profit": "0.50",
                "annualized_yield": "0.20",
                "resolution_at": "2026-12-31T00:00:00Z",
                "clear_signal": True,
                "wallet": "0x2222222222222222222222222222222222222222",
            }],
        }


class _Execution:
    _breaker_open = False
    _cross_breaker_open = False


def test_prediction_state_projects_persisted_n_leg_shadow_summary() -> None:
    class Store(_Store):
        def signal(self, signal_id: str) -> dict[str, object] | None:
            if signal_id != "episode-1":
                return None
            return {
                "signal_id": signal_id,
                "n_leg_shadow": {
                    "latest_fingerprint": "sha256:shadow",
                    "latest_result": {
                        "run_status": "SUCCESS",
                        "decision": "NOT_QUALIFIED",
                        "comparison": "DIFFERENCE",
                    },
                    "first_run_at": "2026-08-15T00:00:00Z",
                    "last_run_at": "2026-08-15T00:01:00Z",
                    "run_count": 2,
                    "qualified_count": 1,
                    "difference_count": 1,
                    "failure_count": 0,
                    "current_differences": {"minimum_profit": {"absolute": "0.10"}},
                    "max_differences": {"minimum_profit": {"absolute": "0.10"}},
                },
            }

    class Monitor(_Monitor):
        def snapshot(self) -> dict[str, object]:
            snapshot = super().snapshot()
            snapshot["opportunities"][0]["signal_episode_id"] = "episode-1"
            return snapshot

    payload = prediction_state_payload(
        store=Store(), monitor=Monitor(), execution=_Execution(), csrf_token="csrf"
    )

    assert payload["opportunities"][0]["n_leg_shadow"]["latest_result"]["comparison"] == "DIFFERENCE"
    assert payload["n_leg_shadow"] == {
        "monitoring": 1,
        "legacy_qualified": 1,
        "completed": 2,
        "differences": 1,
        "failures": 0,
        "last_completed_at": "2026-08-15T00:01:00Z",
    }


@pytest.fixture
def frozen_prediction_inputs() -> dict[str, object]:
    return {
        "store": _Store(),
        "monitor": _Monitor(),
        "execution": _Execution(),
        "cross_venue_monitor": _CrossVenueMonitor(),
    }


@pytest.fixture
def frozen_prediction_state() -> dict[str, object]:
    return json.loads(r'''{
  "auto_eat_stats": {},
  "balances": {"allowance": null, "p_usd": "12.50"},
  "breaker": {"incident": null, "open": false, "status": "ready"},
  "capital_usage": {
    "max_total_unsettled_capital": "0",
    "max_total_unsettled_capital_set": false,
    "current_conservative": "3.25",
    "active_batch_reserved": "0",
    "remaining": null
  },
  "cross_auto": {
    "armed": false,
    "configured_mode": "observe_only",
    "daily_principal": {"current": "0", "limit": "100"},
    "effective_mode": "observe_only",
    "latest_attempt": null,
    "notification_ready": false,
    "pause_reason": "not_armed"
  },
  "cross_venue": {
    "breaker": {"open": false, "scope": "cross_venue"},
    "events": [{"event_id": "cross-event-1", "market_type": "cross_venue_yes_no", "wallet": "0x2222…2222"}],
    "funnel": {
      "arbitrage_space_pairs": 0,
      "clear_signal_pairs": 0,
      "codex_approved_pairs": 0,
      "manual_eligible_pairs": 0,
      "manual_pending_pairs": 0,
      "matched_pairs": 0,
      "monitored_pairs": 0
    },
    "mode": "observe_only",
    "opportunities": [{
      "annualized_yield": "0.20",
      "clear_signal": true,
      "cross_breaker": {"open": false, "scope": "cross_venue"},
      "market_type": "cross_venue_yes_no",
      "minimum_profit": "0.50",
      "opportunity_id": "cross-venue-1",
      "quantity": "5",
      "question": "Cross venue question",
      "resolution_at": "2026-12-31T00:00:00Z",
      "total_max_cost": "4.50",
      "unsettled": {"after": "7.75", "current": "3.25", "limit": "100"},
      "wallet": "0x2222…2222"
    }],
    "status": "ready",
    "unsettled": {"current": "3.25", "limit": "100"}
  },
  "csrf_token": "fixed-csrf",
  "current_execution": null,
  "event_count": 2,
  "events": [
    {"event_id": "cross-event-1", "market_type": "cross_venue_yes_no", "wallet": "0x2222…2222"},
    {
      "actionable": false,
      "event_id": "event-1",
      "event_title": "Same venue question",
      "market_count": 1,
      "markets": [{"market_id": "market-1", "no_token_id": "no-1", "yes_token_id": "yes-1"}],
      "title": "Same venue question"
    }
  ],
  "failure_reason": null,
  "first_live_order": null,
  "health": {"degraded_reasons": [], "status": "healthy"},
  "llm_usage_24h": {"cache_hits": 2, "calls": 4, "failures": 1, "successes": 3},
  "heartbeat": "2026-08-10T01:00:00Z",
  "heartbeat_at": "2026-08-10T01:00:00Z",
  "market_count": 1,
  "masked_wallet": "0x1111…1111",
  "opportunities": [
    {
      "actionable": true,
      "event_title": "Same venue question",
      "market_id": "market-1",
      "market_type": "standard_binary",
      "opportunity_id": "same-venue-1",
      "profit": "1.00",
      "question": "Same venue question",
      "title": "Same venue question",
      "strategy_type": "yes_no",
      "engine_owner": "yes_no",
      "relation_type": "NATIVE_COMPLEMENT",
      "discovery_source": "VENUE_METADATA",
      "leg_count": 2,
      "scope": {"event": "same_event", "venue": "same_venue"},
      "scope_label": "同所 · 同事件",
      "qualification_policy_version": "v1",
      "qualification": {
        "status": "UNKNOWN",
        "checks": [
          {"key": "approved", "label": "已批准", "passed": null, "value": null, "threshold": "APPROVE"},
          {"key": "proof", "label": "证明完整", "passed": null, "value": null, "threshold": true},
          {"key": "min_profit", "label": "最低利润 $1", "passed": true, "value": "1.00", "threshold": "1.00"},
          {"key": "net_edge", "label": "1% 净边际", "passed": null, "value": null, "threshold": "0.01"},
          {"key": "annualized", "label": "15% 年化", "passed": null, "value": null, "threshold": "0.15"},
          {"key": "tenor", "label": "30 天资本释放", "passed": null, "value": null, "threshold": "30"},
          {"key": "not_expired", "label": "未过期", "passed": null, "value": null, "threshold": ">0"}
        ],
        "order_ready": false,
        "order_ready_reason": "资格数据未知"
      }
    },
    {
      "annualized_yield": "0.20",
      "clear_signal": true,
      "cross_breaker": {"open": false, "scope": "cross_venue"},
      "event_title": "Cross venue question",
      "market_type": "cross_venue_yes_no",
      "max_cost": "4.50",
      "minimum_profit": "0.50",
      "opportunity_id": "cross-venue-1",
      "profit": "0.50",
      "quantity": "5",
      "question": "Cross venue question",
      "resolution_at": "2026-12-31T00:00:00Z",
      "title": "Cross venue question",
      "total_max_cost": "4.50",
      "unsettled": {"after": "7.75", "current": "3.25", "limit": "100"},
      "wallet": "0x2222…2222",
      "strategy_type": "yes_no",
      "engine_owner": "yes_no",
      "relation_type": "NATIVE_COMPLEMENT",
      "discovery_source": "VENUE_METADATA",
      "leg_count": 2,
      "scope": {"event": "same_event", "venue": "cross_venue"},
      "scope_label": "跨所 · 同事件",
      "qualification_policy_version": "v1",
      "qualification": {
        "status": "UNKNOWN",
        "checks": [
          {"key": "approved", "label": "已批准", "passed": null, "value": null, "threshold": "APPROVE"},
          {"key": "proof", "label": "证明完整", "passed": null, "value": null, "threshold": true},
          {"key": "min_profit", "label": "最低利润 $1", "passed": false, "value": "0.50", "threshold": "1.00"},
          {"key": "net_edge", "label": "1% 净边际", "passed": null, "value": null, "threshold": "0.01"},
          {"key": "annualized", "label": "15% 年化", "passed": true, "value": "0.20", "threshold": "0.15"},
          {"key": "tenor", "label": "30 天资本释放", "passed": false, "value": null, "threshold": "30"},
          {"key": "not_expired", "label": "未过期", "passed": true, "value": null, "threshold": ">0"}
        ],
        "order_ready": false,
        "order_ready_reason": "资格数据未知"
      }
    }
  ],
  "opportunity_qualification": {
    "status": "NO_QUALIFIED_OPPORTUNITY",
    "no_arbitrage": false,
    "qualified_count": 0,
    "total_count": 2,
    "verified_count": 0,
    "feasible_count": 0,
    "unknown_count": 2
  },
  "policy_limits": {
    "max_cross_unsettled_principal": "100",
    "max_emergency_loss": "2.00",
    "max_normal_cost": "20.00",
    "max_wallet_balance": "65.00",
    "min_estimated_profit": "1.00",
    "min_net_edge": "0.01"
  },
  "qualified_opportunities": [],
  "readiness": {
    "geoblock": "allowed",
    "p_usd_balance": "12.50",
    "relayer": "ready",
    "status": "ready",
    "wallet_address": "0x1111…1111"
  },
  "relation_review": {
    "pending_count": 0,
    "counts": {
      "PENDING_APPROVAL": 0,
      "APPROVED_MODEL_INCOMPLETE": 0,
      "COMPILED_PENDING_ACTIVATION": 0,
      "ACTIVATION_BLOCKED": 0,
      "ACTIVATED": 0,
      "SOURCE_CHANGED_REAPPROVAL": 0
    }
  },
  "relation_discovery": {},
  "signals_24h": 1,
  "stale": false,
  "status": "healthy",
  "token_count": 2,
  "validation_mode": "observe_only",
  "venues": [
    {
      "balance": {"asset": "pUSD", "value": "12.50"},
      "last_success": "2026-08-10T01:00:00Z",
      "mode": "可以交易",
      "reason": null,
      "rest": "ready",
      "venue": "polymarket",
      "wallet": "0x1111…1111",
      "ws": "ready"
    },
    {
      "balance": {"asset": "USDT", "value": null},
      "last_success": null,
      "mode": "只读",
      "reason": null,
      "rest": "ready",
      "venue": "predict.fun",
      "wallet": "0x2222…2222",
      "ws": "ready"
    }
  ],
  "wallet": {"address": "", "masked_address": "0x1111…1111"}
}''')


def test_shared_prediction_read_model_matches_frozen_payload(
    frozen_prediction_inputs: dict[str, object],
    frozen_prediction_state: dict[str, object],
) -> None:
    payload = prediction_state_payload(
        **frozen_prediction_inputs,
        csrf_token="fixed-csrf",
    )

    assert payload == frozen_prediction_state
    assert "0x1111111111111111111111111111111111111111" not in json.dumps(payload)
    assert "0x2222222222222222222222222222222222222222" not in json.dumps(payload)
    assert payload["masked_wallet"] == "0x1111…1111"
    assert payload["venues"][1]["wallet"] == "0x2222…2222"


def _nleg_cross_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "opportunity_id": "cross-verified-1",
        "market_type": "cross_venue_yes_no",
        "question": "Bitcoin above $120k before September?",
        "quantity": "20",
        "total_max_cost": "31.20",
        "minimum_payout": "39.60",
        "minimum_profit": "8.40",
        "annualized_yield": "0.284",
        "remaining_days": "12",
        "resolution_at": "2026-09-01T00:00:00Z",
        "actionable": True,
        "codex_approval": {"decision": "APPROVE", "summary": "deterministic match"},
        "legs": [
            {
                "exchange": "predict.fun",
                "outcome": "YES",
                "net_quantity": "20",
                "max_price": "0.42",
                "max_cost": "16.80",
                "settlement_asset": "USDT",
            },
            {
                "exchange": "polymarket",
                "outcome": "NO",
                "net_quantity": "20",
                "max_price": "0.36",
                "max_cost": "14.40",
                "settlement_asset": "pUSD",
            },
        ],
        "extreme_loss": "-0.80",
        "contract_generation": "1",
    }
    row.update(overrides)
    return row


def _nleg_threshold_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "opportunity_id": "threshold-feasible-1",
        "market_type": "threshold_hedge",
        "question_a": "Hurupay FDV above $50M?",
        "question_b": "Hurupay FDV above $100M?",
        "relation": "B_IMPLIES_A",
        "condition_id_a": "cond-a",
        "condition_id_b": "cond-b",
        "quantity": "20",
        "total_max_cost": "19.00",
        "minimum_payout": "25.18",
        "minimum_profit": "6.18",
        "annualized_yield": "0.189",
        "remaining_days": "20",
        "resolution_at": "2026-09-05T00:00:00Z",
        "actionable": False,
        "llm_status": "approved",
        "extreme_loss": "-1.05",
    }
    row.update(overrides)
    return row


class _NlegExecution:
    _breaker_open = False
    _cross_breaker_open = False

    _fresh_predict_account_snapshot = lambda self: {
        "wallet_address": "0x2222222222222222222222222222222222222222",
        "available_usdt": "40.00",
        "open_orders": [],
        "positions": [],
        "checked_at": "2026-08-16T01:00:00Z",
        "allowance_ready": True,
    }


class _NlegMonitor:
    def __init__(self, rows: list[dict[str, object]], *, stale: bool = False):
        self._rows = rows
        self._stale = stale

    def snapshot(self) -> dict[str, object]:
        return {
            "status": "unavailable" if self._stale else "healthy",
            "health": {
                "status": "unavailable" if self._stale else "healthy",
                "degraded_reasons": ["stale"] if self._stale else [],
            },
            "stale": self._stale,
            "readiness": {
                "status": "ready",
                "wallet_address": "0x1111111111111111111111111111111111111111",
                "p_usd_balance": "60.40",
            },
            "events": [],
            "opportunities": self._rows,
        }


def _nleg_state(rows: list[dict[str, object]], *, stale: bool = False) -> dict[str, object]:
    return prediction_state_payload(
        store=_Store(),
        monitor=_NlegMonitor(rows, stale=stale),
        execution=_NlegExecution(),
        csrf_token="csrf",
    )


def test_nleg_forward_projection_labels_current_opportunities() -> None:
    state = _nleg_state([_nleg_cross_row(), _nleg_threshold_row()])

    cross = state["opportunities"][0]
    threshold = state["opportunities"][1]
    assert cross["strategy_type"] == "yes_no"
    assert cross["engine_owner"] == "yes_no"
    assert cross["relation_type"] == "NATIVE_COMPLEMENT"
    assert cross["discovery_source"] == "VENUE_METADATA"
    assert cross["leg_count"] == 2
    assert cross["scope"] == {"event": "same_event", "venue": "cross_venue"}
    assert cross["scope_label"] == "跨所 · 同事件"
    assert cross["qualification_policy_version"] == "v1"
    assert cross["contract_generation"] == "1"
    assert threshold["strategy_type"] == "llm_hedge"
    assert threshold["engine_owner"] == "llm_hedge"
    assert threshold["relation_type"] == "IMPLIES"
    assert threshold["discovery_source"] == "LLM"
    assert threshold["scope"] == {"event": "same_event", "venue": "same_venue"}
    assert threshold["scope_label"] == "同所 · 同事件"


def test_nleg_solution_projection_is_exposed_and_attached_to_opportunities() -> None:
    class ContractExecution(_NlegExecution):
        def n_leg_mode_contract(self) -> dict[str, object]:
            return {
                "schema_version": "open_trader.prediction_n_leg.mode_contract.v1",
                "contract_generation": 1,
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
                    "s1": {"scope_id": "s1", "capability": "MANUAL_CANARY", "scope_version": 1},
                },
                "enabled_execution_scope_version": [{"scope_id": "s1", "scope_version": 1}],
                "execution_gates": {
                    "breaker_open": False,
                    "incident_active": False,
                    "batch_active": False,
                },
            }

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
    execution_payload = {
        "market_solution_fingerprint": fingerprint(canonical_payload(market)),
        "quantities": market["quantities"],
        "capital_use_units": 31_200_000,
        "reason": "EXECUTABLE",
        "order_ready": False,
        "partial_fill_proof": "PARTIAL_FILL_SAFE",
    }
    state = prediction_state_payload(
        store=_Store(),
        monitor=_NlegMonitor([_nleg_cross_row(component_id="c1")]),
        execution=ContractExecution(),
        csrf_token="csrf",
        n_leg_solutions=[
            {
                "component_id": "c1",
                "scope_id": "s1",
                "market": market,
                "execution": execution_payload,
            }
        ],
    )

    assert state["n_leg_solutions"][0]["component_id"] == "c1"
    assert state["n_leg_solutions"][0]["execution"]["order_ready"] is True
    assert state["n_leg_solutions"][0]["execution"]["reason"] == "MANUAL_CANARY"
    assert state["opportunities"][0]["n_leg_solution"]["execution"]["order_ready"] is True


def test_nleg_metrics_is_exposed_when_provided() -> None:
    state = prediction_state_payload(
        store=_Store(),
        monitor=_NlegMonitor([_nleg_cross_row()]),
        execution=_NlegExecution(),
        csrf_token="csrf",
        n_leg_metrics={
            "compile": {"samples": 2, "p50": 12.0, "p95": 20.0, "worst": 41.0},
            "solve": {"samples": 2, "p50": 380.0, "p95": 610.0, "worst": 910.0},
            "end_to_end": {"samples": 2, "p50": 430.0, "p95": 690.0, "worst": 1020.0},
            "opportunity_survival": {"samples": 2, "p50": 5.0, "p95": 8.2, "worst": 9.0},
            "queue_merge_drop": 12,
            "timeout": 0,
            "stale_reject": 3,
        },
    )

    assert state["n_leg_metrics"]["queue_merge_drop"] == 12
    assert state["n_leg_metrics"]["stale_reject"] == 3


def test_qualified_verified_cross_opportunity_is_order_ready() -> None:
    state = _nleg_state([_nleg_cross_row()])
    row = state["opportunities"][0]
    qualification = row["qualification"]

    assert qualification["status"] == "QUALIFIED_VERIFIED"
    assert qualification["order_ready"] is True
    assert qualification["order_ready_reason"] == ""
    checks = {item["key"]: item for item in qualification["checks"]}
    assert checks["approved"]["passed"] is True
    assert checks["proof"]["passed"] is True
    assert checks["min_profit"]["passed"] is True
    assert checks["net_edge"]["passed"] is True
    assert checks["annualized"]["passed"] is True
    assert checks["tenor"]["passed"] is True
    assert checks["not_expired"]["passed"] is True
    assert row["extreme_loss"] == "-0.80"
    assert state["opportunity_qualification"]["status"] == "QUALIFIED"
    assert state["qualified_opportunities"][0]["opportunity_id"] == "cross-verified-1"


def test_llm_threshold_stays_feasible_and_insufficient_balance_is_not_no_arbitrage() -> None:
    row = _nleg_threshold_row(total_max_cost="70.00")
    state = _nleg_state([row])
    qualification = state["opportunities"][0]["qualification"]

    assert qualification["status"] == "QUALIFIED_FEASIBLE"
    assert qualification["order_ready"] is False
    assert "余额不足" in qualification["order_ready_reason"]
    assert state["opportunity_qualification"]["status"] == "QUALIFIED"
    assert state["opportunity_qualification"]["no_arbitrage"] is False
    assert "NO_ARBITRAGE" not in state["opportunity_qualification"]["status"]


def test_qualification_not_qualified_and_unknown_are_distinguished() -> None:
    low_profit = _nleg_threshold_row(minimum_profit="0.50")
    missing_proof = _nleg_cross_row(codex_approval={})
    state = _nleg_state([low_profit, missing_proof])
    by_id = {
        str(row["opportunity_id"]): row["qualification"]["status"]
        for row in state["opportunities"]
    }

    assert by_id["threshold-feasible-1"] == "NOT_QUALIFIED"
    assert by_id["cross-verified-1"] == "UNKNOWN"
    assert state["qualified_opportunities"] == []
    assert state["opportunity_qualification"]["status"] == "NO_QUALIFIED_OPPORTUNITY"


def test_opportunity_qualification_summary_is_unknown_when_stale() -> None:
    state = _nleg_state([_nleg_cross_row()], stale=True)
    assert state["opportunity_qualification"]["status"] == "UNKNOWN"


def test_nleg_history_keeps_strategy_type_without_forward_writeback() -> None:
    class Store(_Store):
        def signal_history(self, _window: str) -> list[dict[str, object]]:
            return [{
                "signal_id": "signal-legacy",
                "opportunity_id": "same-venue-1",
                "started_at": "2026-08-10T01:02:03Z",
                "question": "Legacy question",
                "strategy_type": "yes_no",
            }]

    payload = prediction_history_payload(
        Store(),
        kind="signals",
        limit=100,
        offset=0,
        monitor=_NlegMonitor([]),
        execution=_NlegExecution(),
        cross_venue_monitor=None,
    )
    item = payload["items"][0]
    assert item["strategy_type"] == "yes_no"
    assert "relation_type" not in item
    assert "engine_owner" not in item
    assert "qualification" not in item


class _RelationCatalog:
    def review_counts(self) -> dict[str, object]:
        return {
            "counts": {
                "PENDING_APPROVAL": 2,
                "APPROVED_MODEL_INCOMPLETE": 1,
                "COMPILED_PENDING_ACTIVATION": 1,
                "ACTIVATION_BLOCKED": 1,
                "ACTIVATED": 1,
                "SOURCE_CHANGED_REAPPROVAL": 1,
            },
            "pending_count": 2,
        }


def test_relation_review_projects_six_state_counts_without_rows_or_secrets() -> None:
    state = prediction_state_payload(
        store=_Store(),
        monitor=_NlegMonitor([]),
        execution=_NlegExecution(),
        csrf_token="csrf",
        relation_catalog=_RelationCatalog(),
    )
    review = state["relation_review"]

    assert review == {
        "pending_count": 2,
        "counts": {
            "PENDING_APPROVAL": 2,
            "APPROVED_MODEL_INCOMPLETE": 1,
            "COMPILED_PENDING_ACTIVATION": 1,
            "ACTIVATION_BLOCKED": 1,
            "ACTIVATED": 1,
            "SOURCE_CHANGED_REAPPROVAL": 1,
        },
    }
    serialized = json.dumps(review)
    for secret in ("terminal_states", "payouts", "capital_release", "payout", "preview", "items"):
        assert secret not in serialized


def test_relation_review_empty_when_catalog_missing() -> None:
    state = _nleg_state([])
    assert state["relation_review"] == {
        "pending_count": 0,
        "counts": {
            "PENDING_APPROVAL": 0,
            "APPROVED_MODEL_INCOMPLETE": 0,
            "COMPILED_PENDING_ACTIVATION": 0,
            "ACTIVATION_BLOCKED": 0,
            "ACTIVATED": 0,
            "SOURCE_CHANGED_REAPPROVAL": 0,
        },
    }


class _NlegContractExecution(_NlegExecution):
    def n_leg_mode_contract(self) -> dict[str, object]:
        return {
            "schema_version": "open_trader.prediction_n_leg.mode_contract.v1",
            "contract_generation": 1,
            "mode": "MANUAL",
            "qualification_policy_version": 1,
            "qualification_policy": {
                "min_profit_usd": "1.00",
                "min_net_margin": "0.01",
                "min_annualized_return": "0.15",
                "max_capital_release_days": 30,
            },
            "safety_config_version": 1,
            "safety_config": {"max_total_unsettled_capital_units": 60000000},
            "execution_scopes": {},
            "enabled_execution_scope_version": [],
            "execution_gates": {
                "breaker_open": False,
                "incident_active": False,
                "batch_active": False,
            },
        }


def test_nleg_contract_and_capital_usage_are_projected_read_only() -> None:
    state = prediction_state_payload(
        store=_Store(),
        monitor=_NlegMonitor([]),
        execution=_NlegContractExecution(),
        csrf_token="csrf",
    )

    n_leg = state["n_leg"]
    assert n_leg["mode"] == "MANUAL"
    assert n_leg["contract_generation"] == 1
    assert n_leg["qualification_policy_version"] == 1
    assert n_leg["execution_gates"] == {
        "breaker_open": False,
        "incident_active": False,
        "batch_active": False,
    }
    assert state["capital_usage"] == {
        "max_total_unsettled_capital": "60",
        "max_total_unsettled_capital_set": True,
        "current_conservative": "3.25",
        "active_batch_reserved": "0",
        "remaining": "56.75",
    }


def test_capital_usage_defaults_to_unset_without_remaining() -> None:
    state = _nleg_state([])
    assert state["capital_usage"] == {
        "max_total_unsettled_capital": "0",
        "max_total_unsettled_capital_set": False,
        "current_conservative": "3.25",
        "active_batch_reserved": "0",
        "remaining": None,
    }


@pytest.mark.parametrize(("kind", "expected"), (
    ("signals", {
        "kind": "signals", "items": [{
            "signal_id": "signal-1", "opportunity_id": "same-venue-1",
            "market_id": "market-1", "started_at": "2026-08-10T01:02:03Z",
            "question": "Same venue question", "wallet_address": "0x1111…1111",
            "occurred_at": "2026-08-10T01:02:03Z", "event_title": "Same venue question",
            "duration": "进行中", "actionable_now": False, "live_profit": None,
        }], "total": 1, "limit": 10, "offset": 0, "has_more": False,
    }),
    ("executions", {
        "kind": "executions", "items": [{
            "execution_id": "execution-1", "state": "complete",
            "updated_at": "2026-08-10T01:03:04Z", "question": "Same venue question",
            "actual_cost": "9.50", "wallet": "0x1111…1111", "status": "complete",
            "completed_at": "2026-08-10T01:03:04Z", "event_title": "Same venue question",
        }], "total": 1, "limit": 10, "offset": 0, "has_more": False,
    }),
    ("incidents", {
        "kind": "incidents", "items": [{
            "incident_id": "incident-1", "state": "resolved",
            "created_at": "2026-08-10T01:04:05Z", "question": "Same venue question",
            "reason": "reconciled", "wallet": "0x1111…1111", "status": "resolved",
            "happened_at": "2026-08-10T01:04:05Z", "event_title": "Same venue question",
        }], "total": 1, "limit": 10, "offset": 0, "has_more": False,
    }),
))
def test_shared_prediction_read_model_projects_frozen_history_by_kind(
    frozen_prediction_inputs: dict[str, object], kind: str, expected: dict[str, object]
) -> None:
    store = frozen_prediction_inputs["store"]
    assert prediction_history_payload(
        store,
        kind=kind,
        limit=10,
        offset=0,
        monitor=frozen_prediction_inputs["monitor"],
        execution=frozen_prediction_inputs["execution"],
        cross_venue_monitor=frozen_prediction_inputs["cross_venue_monitor"],
    ) == expected


def test_shared_prediction_read_model_rejects_unknown_history_kind(
    frozen_prediction_inputs: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="kind must be signals, executions, or incidents"):
        prediction_history_payload(
            frozen_prediction_inputs["store"], kind="unknown", limit=10, offset=0
        )


def test_state_read_path_never_performs_live_predict_fetch() -> None:
    """#93: the read path resolves only the execution cache read seam.

    Before #93 each state request performed two live on-chain/REST snapshot
    fetches on the HTTP request thread (10-28s in production). Now a live
    ``_predict_trading.account_snapshot`` must never be reached.
    """

    class LivePredictTrading:
        def __init__(self) -> None:
            self.calls = 0

        def account_snapshot(self) -> dict[str, object]:
            self.calls += 1
            raise AssertionError("live predict fetch must not run on the read path")

    class CachedExecution(_NlegExecution):
        def __init__(self) -> None:
            self._predict_trading = LivePredictTrading()

    execution = CachedExecution()
    state = prediction_state_payload(
        store=_Store(),
        monitor=_NlegMonitor([_nleg_cross_row()]),
        execution=execution,
        csrf_token="csrf",
    )
    again = prediction_state_payload(
        store=_Store(),
        monitor=_NlegMonitor([_nleg_cross_row()]),
        execution=execution,
        csrf_token="csrf",
    )
    assert state["status"] != "unavailable"
    assert again["status"] != "unavailable"
    assert execution._predict_trading.calls == 0


def test_read_model_returns_empty_without_direct_predict_client_fallback() -> None:
    """#93: an execution object without the cache seam yields {}, not a live fetch."""
    from open_trader.prediction_read_model import _prediction_predict_account_snapshot

    class LivePredictTrading:
        def __init__(self) -> None:
            self.calls = 0

        def account_snapshot(self) -> dict[str, object]:
            self.calls += 1
            return {
                "wallet_address": "0x2222222222222222222222222222222222222222",
                "available_usdt": "40.00",
                "open_orders": [],
                "positions": [],
                "checked_at": "2026-08-16T01:00:00Z",
                "allowance_ready": True,
            }

    class ClientOnlyExecution:
        _breaker_open = False
        _cross_breaker_open = False
        _predict_trading = LivePredictTrading()

    execution = ClientOnlyExecution()
    assert _prediction_predict_account_snapshot(execution) == {}
    assert execution._predict_trading.calls == 0
    assert _prediction_predict_account_snapshot(_NlegExecution()) != {}
