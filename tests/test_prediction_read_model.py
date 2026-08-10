from __future__ import annotations

import json
from decimal import Decimal

import pytest

from open_trader.dashboard_web import (
    _prediction_history_payload,
    _prediction_state_payload,
)
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
def frozen_prediction_state(
    frozen_prediction_inputs: dict[str, object],
) -> dict[str, object]:
    return _prediction_state_payload(**frozen_prediction_inputs, csrf_token="fixed-csrf")


def test_shared_prediction_read_model_matches_frozen_legacy_payload(
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


@pytest.mark.parametrize("kind", ("signals", "executions", "incidents"))
def test_shared_prediction_read_model_matches_legacy_history_by_kind(
    frozen_prediction_inputs: dict[str, object], kind: str
) -> None:
    store = frozen_prediction_inputs["store"]
    expected = _prediction_history_payload(
        store,
        kind=kind,
        limit=10,
        offset=0,
        monitor=frozen_prediction_inputs["monitor"],
        execution=frozen_prediction_inputs["execution"],
        cross_venue_monitor=frozen_prediction_inputs["cross_venue_monitor"],
    )

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
