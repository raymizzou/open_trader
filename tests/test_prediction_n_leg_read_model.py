"""Issue #85: read-model projection of Market/Execution solutions."""

from __future__ import annotations

from datetime import UTC, datetime

from open_trader.prediction_market_solution import (
    EXECUTABLE_REASON,
    INSUFFICIENT_FUNDS_REASON,
    MarketSolution,
)
from open_trader.prediction_n_leg import ActionQuantity, canonical_payload, fingerprint
from open_trader.prediction_n_leg_read_model import (
    EXECUTION_FINGERPRINT_MISMATCH,
    PARTIAL_FILL_PROOF_REQUIRED,
    SCOPE_OBSERVE_ONLY,
    UNSETTLED_CAP_EXCEEDED,
    project_n_leg_solution,
)


def _market(
    *,
    component_id: str = "c1",
    quantities: tuple[ActionQuantity, ...] = (
        ActionQuantity("a-yes", 2),
        ActionQuantity("a-no", 2),
    ),
) -> dict[str, object]:
    return canonical_payload(
        MarketSolution(
            component_id=component_id,
            structure_fingerprint="sha256:struct",
            quote_fingerprint="sha256:quote",
            quantities=quantities,
            guaranteed_profit_units=20,
            bounded_cost_units=180,
            bounded_payout_units=200,
            capital_release_at=datetime(2026, 8, 12, tzinfo=UTC),
            global_search_closed=False,
            verification_fingerprint="sha256:verify",
        )
    )


def _execution(
    market: dict[str, object],
    *,
    reason: str = EXECUTABLE_REASON,
    capital_use_units: int = 180,
    market_solution_fingerprint: object = None,
) -> dict[str, object]:
    return {
        "market_solution_fingerprint": (
            fingerprint(canonical_payload(market))
            if market_solution_fingerprint is None
            else market_solution_fingerprint
        ),
        "quantities": market["quantities"],
        "capital_use_units": capital_use_units,
        "reason": reason,
        "order_ready": False,
        "partial_fill_proof": "UNKNOWN",
    }


def _manual_canary_scope() -> dict[str, object]:
    return {
        "capability": "MANUAL_CANARY",
        "order_ready": True,
        "reason": "MANUAL_CANARY",
        "action": "manual_confirm",
    }


def test_projection_returns_none_without_market_solution() -> None:
    assert (
        project_n_leg_solution(
            market=None,
            execution=None,
            scope=_manual_canary_scope(),
            max_total_unsettled_capital_units=1000,
        )
        is None
    )


def test_projection_emits_market_fields_and_per_leg_display() -> None:
    market = _market()
    legs = [
        {"action_id": "a-yes", "venue": "polymarket", "outcome": "YES", "max_price": "0.42", "max_cost": "16.80", "settlement_asset": "pUSD"},
        {"action_id": "a-no", "venue": "polymarket", "outcome": "NO", "max_price": "0.36", "max_cost": "14.40", "settlement_asset": "pUSD"},
    ]

    item = project_n_leg_solution(
        market=market,
        execution=_execution(market),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        legs=legs,
    )

    assert item is not None
    assert item["component_id"] == "c1"
    assert item["market"]["minimum_profit"] == "0.00002"
    assert item["market"]["maximum_cost"] == "0.00018"
    assert item["market"]["capital_release_at"] == "2026-08-12T00:00:00Z"
    assert item["market"]["structure_fingerprint"] == "sha256:struct"
    assert item["market"]["quote_fingerprint"] == "sha256:quote"
    assert item["market"]["verification_fingerprint"] == "sha256:verify"
    by_action = {leg["action_id"]: leg for leg in item["market"]["legs"]}
    assert by_action["a-yes"]["quantity_lots"] == 2
    assert by_action["a-yes"]["max_price"] == "0.42"
    assert by_action["a-yes"]["max_cost"] == "16.80"
    assert by_action["a-no"]["max_price"] == "0.36"


def test_projection_manual_canary_is_order_ready() -> None:
    market = _market()
    item = project_n_leg_solution(
        market=market,
        execution=_execution(market),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
    )

    assert item is not None
    assert item["execution"]["order_ready"] is True
    assert item["execution"]["reason"] == "MANUAL_CANARY"
    assert item["execution"]["would_submit"] is True
    assert item["execution"]["execution_solution_fingerprint"] == fingerprint(
        canonical_payload(_execution(market))
    )


def test_projection_without_execution_is_not_order_ready() -> None:
    market = _market()
    item = project_n_leg_solution(
        market=market,
        execution=None,
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
    )

    assert item is not None
    assert item["execution"]["order_ready"] is False
    assert item["execution"]["reason"] == PARTIAL_FILL_PROOF_REQUIRED
    assert item["execution"]["would_submit"] is False
    assert item["execution"]["execution_solution_fingerprint"] is None


def test_projection_observe_only_scope_blocks_ready() -> None:
    market = _market()
    item = project_n_leg_solution(
        market=market,
        execution=_execution(market),
        scope={"capability": "OBSERVE_ONLY", "order_ready": False, "reason": "SCOPE_OBSERVE_ONLY", "action": None},
        max_total_unsettled_capital_units=1000,
    )

    assert item is not None
    assert item["execution"]["order_ready"] is False
    assert item["execution"]["reason"] == SCOPE_OBSERVE_ONLY


def test_projection_fingerprint_mismatch_invalidates_qualification() -> None:
    market = _market()
    item = project_n_leg_solution(
        market=market,
        execution=_execution(market, market_solution_fingerprint="sha256:stale"),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
    )

    assert item is not None
    assert item["execution"]["order_ready"] is False
    assert item["execution"]["reason"] == EXECUTION_FINGERPRINT_MISMATCH
    assert item["market"]["minimum_profit"] is not None


def test_projection_non_executable_execution_reason_blocks_ready() -> None:
    market = _market()
    item = project_n_leg_solution(
        market=market,
        execution=_execution(market, reason=INSUFFICIENT_FUNDS_REASON),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
    )

    assert item is not None
    assert item["execution"]["order_ready"] is False
    assert item["execution"]["reason"] == INSUFFICIENT_FUNDS_REASON


def test_projection_over_unsettled_cap_blocks_ready_but_keeps_market() -> None:
    market = _market()
    item = project_n_leg_solution(
        market=market,
        execution=_execution(market, capital_use_units=180),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=100,
        total_unsettled_capital_units=0,
    )

    assert item is not None
    assert item["execution"]["order_ready"] is False
    assert item["execution"]["reason"] == UNSETTLED_CAP_EXCEEDED
    assert item["execution"]["projected_total_units"] == 180
    assert item["execution"]["max_total_unsettled_capital_units"] == 100
    assert item["market"]["minimum_profit"] is not None


def test_projection_within_cap_includes_unsettled_units() -> None:
    market = _market()
    item = project_n_leg_solution(
        market=market,
        execution=_execution(market, capital_use_units=180),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        total_unsettled_capital_units=50,
    )

    assert item is not None
    assert item["execution"]["order_ready"] is True
    assert item["execution"]["total_unsettled_capital_units"] == 50


def test_projection_scope_ready_false_keeps_execution_reason() -> None:
    market = _market()
    item = project_n_leg_solution(
        market=market,
        execution=_execution(market),
        scope={"capability": "AUTO_ELIGIBLE", "order_ready": False, "reason": "SCOPE_NOT_ENABLED", "action": None},
        max_total_unsettled_capital_units=1000,
    )

    assert item is not None
    assert item["execution"]["order_ready"] is False
    assert item["execution"]["reason"] == "SCOPE_NOT_ENABLED"
