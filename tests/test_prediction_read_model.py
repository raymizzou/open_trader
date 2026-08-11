from __future__ import annotations

import json
from decimal import Decimal

import pytest

from open_trader.prediction_read_model import (
    prediction_history_payload,
    prediction_state_payload,
)


class _Store:
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
      "title": "Same venue question"
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
      "wallet": "0x2222…2222"
    }
  ],
  "policy_limits": {
    "max_cross_unsettled_principal": "100",
    "max_emergency_loss": "2.00",
    "max_normal_cost": "20.00",
    "max_wallet_balance": "65.00",
    "min_estimated_profit": "1.00",
    "min_net_edge": "0.01"
  },
  "readiness": {
    "geoblock": "allowed",
    "p_usd_balance": "12.50",
    "relayer": "ready",
    "status": "ready",
    "wallet_address": "0x1111…1111"
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
