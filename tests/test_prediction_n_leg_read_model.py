"""Issue #85: read-model projection of Market/Execution solutions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


_NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _market(
    *,
    component_id: str = "c1",
    quantities: tuple[ActionQuantity, ...] = (
        ActionQuantity("a-yes", 2),
        ActionQuantity("a-no", 2),
    ),
    profit_units: int = 20,
    cost_units: int = 180,
    payout_units: int = 200,
    capital_release_at: datetime = datetime(2026, 8, 12, tzinfo=UTC),
    global_search_closed: bool = False,
) -> dict[str, object]:
    return canonical_payload(
        MarketSolution(
            component_id=component_id,
            structure_fingerprint="sha256:struct",
            quote_fingerprint="sha256:quote",
            quantities=quantities,
            guaranteed_profit_units=profit_units,
            bounded_cost_units=cost_units,
            bounded_payout_units=payout_units,
            capital_release_at=capital_release_at,
            global_search_closed=global_search_closed,
            verification_fingerprint="sha256:verify",
        )
    )


def _execution(
    market: dict[str, object],
    *,
    reason: str = EXECUTABLE_REASON,
    capital_use_units: int = 180,
    market_solution_fingerprint: object = None,
    proof_status: str = "PARTIAL_FILL_SAFE",
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
        "partial_fill_proof": proof_status,
    }


def _manual_canary_scope() -> dict[str, object]:
    return {
        "capability": "MANUAL_CANARY",
        "order_ready": True,
        "reason": "MANUAL_CANARY",
        "action": "manual_confirm",
    }


def test_projection_min_profit_boundary() -> None:
    market = _market(profit_units=1_000_000, cost_units=99_000_000, payout_units=100_000_000)
    item = project_n_leg_solution(
        market=market,
        execution=_execution(market),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
    )

    assert item is not None
    checks = {row["key"]: row for row in item["qualification"]["checks"]}
    assert checks["min_profit"]["passed"] is True
    assert checks["min_profit"]["value"] == "1"
    assert checks["min_profit"]["threshold"] == "1.00"

    failing = _market(profit_units=999_999, cost_units=99_000_000, payout_units=100_000_000)
    item = project_n_leg_solution(
        market=failing,
        execution=_execution(failing),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
    )

    assert item is not None
    checks = {row["key"]: row for row in item["qualification"]["checks"]}
    assert checks["min_profit"]["passed"] is False


def test_projection_net_margin_boundary() -> None:
    exact = _market(profit_units=1_000_000, cost_units=99_000_000, payout_units=100_000_000)
    item = project_n_leg_solution(
        market=exact,
        execution=_execution(exact),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
    )

    assert item is not None
    checks = {row["key"]: row for row in item["qualification"]["checks"]}
    assert checks["net_margin"]["passed"] is True
    assert item["qualification"]["net_margin"] == "0.01"

    below = _market(profit_units=1_000_000, cost_units=100_000_000, payout_units=101_000_000)
    item = project_n_leg_solution(
        market=below,
        execution=_execution(below),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
    )

    assert item is not None
    checks = {row["key"]: row for row in item["qualification"]["checks"]}
    assert checks["net_margin"]["passed"] is False


def test_projection_annualized_boundary_and_day_clamp() -> None:
    at_15 = _market(
        profit_units=15_000_000,
        cost_units=85_000_000,
        payout_units=100_000_000,
        capital_release_at=_NOW + timedelta(days=365),
    )
    item = project_n_leg_solution(
        market=at_15,
        execution=_execution(at_15),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
    )

    assert item is not None
    checks = {row["key"]: row for row in item["qualification"]["checks"]}
    assert checks["annualized_return"]["passed"] is True
    assert item["qualification"]["annualized_return"] == "0.15"

    below_15 = _market(
        profit_units=14_900_000,
        cost_units=85_100_000,
        payout_units=100_000_000,
        capital_release_at=_NOW + timedelta(days=365),
    )
    item = project_n_leg_solution(
        market=below_15,
        execution=_execution(below_15),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
    )

    assert item is not None
    checks = {row["key"]: row for row in item["qualification"]["checks"]}
    assert checks["annualized_return"]["passed"] is False

    two_hours = _market(
        profit_units=15_000_000,
        cost_units=85_000_000,
        payout_units=100_000_000,
        capital_release_at=_NOW + timedelta(hours=2),
    )
    item = project_n_leg_solution(
        market=two_hours,
        execution=_execution(two_hours),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
    )

    assert item is not None
    assert item["qualification"]["capital_release_days"] == 1


def test_projection_capital_release_window() -> None:
    def _release_item(release: datetime):
        market = _market(
            profit_units=15_000_000,
            cost_units=85_000_000,
            payout_units=100_000_000,
            capital_release_at=release,
        )
        return project_n_leg_solution(
            market=market,
            execution=_execution(market),
            scope=_manual_canary_scope(),
            max_total_unsettled_capital_units=1000,
            now=_NOW,
        )

    item = _release_item(_NOW + timedelta(days=30))
    assert item is not None
    checks = {row["key"]: row for row in item["qualification"]["checks"]}
    assert checks["capital_release"]["passed"] is True
    assert item["qualification"]["capital_release_days"] == 30

    item = _release_item(_NOW + timedelta(days=30, minutes=6))
    assert item is not None
    checks = {row["key"]: row for row in item["qualification"]["checks"]}
    assert checks["capital_release"]["passed"] is False
    assert item["qualification"]["capital_release_days"] == 31

    item = _release_item(_NOW - timedelta(seconds=1))
    assert item is not None
    checks = {row["key"]: row for row in item["qualification"]["checks"]}
    assert checks["capital_release"]["passed"] is False
    assert item["qualification"]["capital_release_days"] is None


def test_projection_unknown_fail_closed() -> None:
    missing_release = _market(
        profit_units=2_000_000,
        cost_units=95_000_000,
        payout_units=100_000_000,
        capital_release_at=None,
    )
    item = project_n_leg_solution(
        market=missing_release,
        execution=_execution(missing_release),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
    )

    assert item is not None
    checks = {row["key"]: row for row in item["qualification"]["checks"]}
    assert checks["capital_release"]["passed"] is None
    assert item["qualification"]["status"] == "UNKNOWN"

    missing_cost = _market(
        profit_units=2_000_000,
        cost_units=None,
        payout_units=100_000_000,
        capital_release_at=_NOW + timedelta(days=20),
    )
    item = project_n_leg_solution(
        market=missing_cost,
        execution=_execution(missing_cost),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
    )

    assert item is not None
    assert item["qualification"]["status"] == "UNKNOWN"

    leg_missing_venue = _market(
        profit_units=2_000_000,
        cost_units=95_000_000,
        payout_units=100_000_000,
        capital_release_at=_NOW + timedelta(days=20),
    )
    item = project_n_leg_solution(
        market=leg_missing_venue,
        execution=_execution(leg_missing_venue),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
        balance_snapshot={
            "polymarket": {"available": "50.00", "allowance": "50.00"},
        },
        legs=[{"action_id": "a-yes", "max_cost": "10.00"}],
    )

    assert item is not None
    assert item["funding"]["status"] == "UNKNOWN"


def test_projection_fully_qualified_and_would_submit() -> None:
    def _qualified(**overrides: object) -> dict[str, object]:
        kwargs = {
            "profit_units": 2_000_000,
            "cost_units": 95_000_000,
            "payout_units": 100_000_000,
            "capital_release_at": _NOW + timedelta(days=20),
        }
        kwargs.update(overrides)
        return _market(**kwargs)

    closed = _qualified(global_search_closed=True)
    item = project_n_leg_solution(
        market=closed,
        execution=_execution(closed),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
    )

    assert item is not None
    assert item["qualification"]["status"] == "QUALIFIED_VERIFIED"
    assert all(row["passed"] is True for row in item["qualification"]["checks"])
    assert item["qualification"]["annualized_return"] == "0.365"
    assert item["qualification"]["optimality"] == "OPTIMAL"
    assert item["qualification"]["worst_case"] == {
        "minimum_payout": "100",
        "maximum_cost": "95",
        "guaranteed_profit": "2",
    }
    assert item["execution"]["would_submit"] is True

    open_search = _qualified(global_search_closed=False)
    item = project_n_leg_solution(
        market=open_search,
        execution=_execution(open_search),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
    )

    assert item is not None
    assert item["qualification"]["optimality"] == "QUALIFIED_FEASIBLE"

    observe_only = _qualified(global_search_closed=False)
    item = project_n_leg_solution(
        market=observe_only,
        execution=_execution(observe_only),
        scope=None,
        max_total_unsettled_capital_units=1000,
        now=_NOW,
    )

    assert item is not None
    assert item["execution"]["order_ready"] is False
    assert item["execution"]["reason"] == SCOPE_OBSERVE_ONLY
    assert item["execution"]["would_submit"] is True


def _qualified_item(
    *,
    balance_snapshot: dict[str, dict[str, str]] | None = None,
) -> dict[str, object] | None:
    market = _market(
        profit_units=2_000_000,
        cost_units=95_000_000,
        payout_units=100_000_000,
        capital_release_at=_NOW + timedelta(days=20),
        global_search_closed=True,
    )
    return project_n_leg_solution(
        market=market,
        execution=_execution(market),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
        balance_snapshot=balance_snapshot,
        legs=[
            {"action_id": "a-yes", "venue": "polymarket", "max_cost": "10.00"},
            {"action_id": "a-no", "venue": "predict.fun", "max_cost": "5.00"},
        ],
    )


def test_projection_insufficient_balance_blocks_executable() -> None:
    item = _qualified_item(
        balance_snapshot={
            "polymarket": {"available": "50.00", "allowance": "50.00"},
            "predict.fun": {"available": "4.00", "allowance": "50.00"},
        }
    )

    assert item is not None
    assert item["main_list"] is True
    assert item["funding"]["status"] == "INSUFFICIENT"
    predict_row = item["funding"]["venues"]["predict.fun"]
    assert predict_row["reasons"] == ["INSUFFICIENT_BALANCE"]
    assert predict_row["required"] == "5.00"
    assert predict_row["balance_ok"] is False
    assert predict_row["allowance_ok"] is True
    polymarket_row = item["funding"]["venues"]["polymarket"]
    assert polymarket_row["reasons"] == []
    assert item["executable"] is False
    assert item["blocked_reason"] == "INSUFFICIENT_BALANCE"


def test_projection_insufficient_allowance_is_independent_reason() -> None:
    allowance_only = _qualified_item(
        balance_snapshot={
            "polymarket": {"available": "50.00", "allowance": "50.00"},
            "predict.fun": {"available": "5.00", "allowance": "3.00"},
        }
    )

    assert allowance_only is not None
    predict_row = allowance_only["funding"]["venues"]["predict.fun"]
    assert predict_row["reasons"] == ["INSUFFICIENT_ALLOWANCE"]
    assert predict_row["balance_ok"] is True
    assert predict_row["allowance_ok"] is False
    assert allowance_only["funding"]["status"] == "INSUFFICIENT"
    assert allowance_only["executable"] is False
    assert allowance_only["blocked_reason"] == "INSUFFICIENT_ALLOWANCE"

    both_low = _qualified_item(
        balance_snapshot={
            "polymarket": {"available": "50.00", "allowance": "50.00"},
            "predict.fun": {"available": "4.00", "allowance": "3.00"},
        }
    )

    assert both_low is not None
    predict_row = both_low["funding"]["venues"]["predict.fun"]
    assert predict_row["reasons"] == [
        "INSUFFICIENT_BALANCE",
        "INSUFFICIENT_ALLOWANCE",
    ]
    assert both_low["blocked_reason"] == "INSUFFICIENT_BALANCE"


def test_projection_missing_venue_snapshot_is_funding_unknown() -> None:
    item = _qualified_item(
        balance_snapshot={
            "polymarket": {"available": "50.00", "allowance": "50.00"},
        }
    )

    assert item is not None
    assert item["main_list"] is True
    assert item["funding"]["status"] == "UNKNOWN"
    assert item["executable"] is None
    assert item["blocked_reason"] == "FUNDING_UNKNOWN"


def test_projection_not_qualified_composition() -> None:
    market = _market(
        profit_units=500_000,
        cost_units=99_500_000,
        payout_units=100_000_000,
        capital_release_at=_NOW + timedelta(days=20),
        global_search_closed=True,
    )
    item = project_n_leg_solution(
        market=market,
        execution=_execution(market),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=_NOW,
        balance_snapshot={
            "polymarket": {"available": "50.00", "allowance": "50.00"},
            "predict.fun": {"available": "50.00", "allowance": "50.00"},
        },
        legs=[
            {"action_id": "a-yes", "venue": "polymarket", "max_cost": "10.00"},
            {"action_id": "a-no", "venue": "predict.fun", "max_cost": "5.00"},
        ],
    )

    assert item is not None
    assert item["qualification"]["status"] == "NOT_QUALIFIED"
    assert item["main_list"] is False
    assert item["executable"] is False
    assert item["blocked_reason"] == "NOT_QUALIFIED"


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
    # #104: would_submit requires qualification.status == QUALIFIED_VERIFIED,
    # so this test uses a market that passes the default policy.
    market = _market(
        profit_units=2_000_000,
        cost_units=95_000_000,
        payout_units=100_000_000,
        capital_release_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = project_n_leg_solution(
        market=market,
        execution=_execution(market),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        now=datetime(2026, 8, 1, tzinfo=UTC),
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


def test_projection_unknown_proof_requires_proof() -> None:
    market = _market()
    item = project_n_leg_solution(
        market=market,
        execution=_execution(market, proof_status="UNKNOWN"),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
    )

    assert item is not None
    assert item["execution"]["order_ready"] is False
    assert item["execution"]["reason"] == PARTIAL_FILL_PROOF_REQUIRED
    assert item["execution"]["partial_fill_proof"] == "UNKNOWN"


def test_projection_unsafe_proof_blocks_ready() -> None:
    market = _market()
    proof_payload = {
        "status": "PARTIAL_FILL_UNSAFE",
        "solver_termination": "CLOSED",
        "verifier_status": "QUALIFIED_VERIFIED",
        "solver_lower_bound": 490_000,
        "solver_upper_bound": 490_000,
        "max_partial_fill_loss": 0,
        "fingerprint": "sha256:proof",
    }
    item = project_n_leg_solution(
        market=market,
        execution=_execution(market, proof_status="PARTIAL_FILL_SAFE"),
        scope=_manual_canary_scope(),
        max_total_unsettled_capital_units=1000,
        partial_fill_proof=proof_payload,
    )

    assert item is not None
    assert item["execution"]["order_ready"] is False
    assert item["execution"]["reason"] == "PARTIAL_FILL_UNSAFE"
    # The explicit record payload wins over the execution status string.
    assert item["execution"]["partial_fill_proof"] == "PARTIAL_FILL_UNSAFE"
    assert item["execution"]["partial_fill_upper_bound_units"] == 490_000
    assert item["execution"]["partial_fill_cap_units"] == 0
    assert item["execution"]["partial_fill_proof_fingerprint"] == "sha256:proof"
