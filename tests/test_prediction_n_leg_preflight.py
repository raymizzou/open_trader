"""Issue #64 Slice 3: preflight of the frozen solution against fresh books.

The source of truth for C1 is the #117 hand-worked sample (asks 0.40 + 0.40,
20 lots per leg, one leg charging 500 bps): per-share fee
0.05 x 400,000 x 600,000 / 1e6 = 12,000 units, 20 lots -> 240,000 units, so
the fee-bearing leg costs 8,240,000, the free leg 8,000,000, total
16,240,000 against a 20,000,000 payout bound -> net profit 3,760,000 units.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from open_trader.prediction_arbitrage import BookLevel
from open_trader.prediction_n_leg import (
    ActionPayout,
    ActionQuantity,
    ActionSide,
    CandidateAction,
    ExecutableCostSlice,
    SettlementObservationKey,
    TerminalAtom,
    TerminalStateSet,
    canonical_payload,
)
from open_trader.prediction_n_leg_preflight import preflight
from open_trader.prediction_snapshot_scheduler import (
    ComponentSnapshot,
    LegBook,
    SnapshotLeg,
)
from test_prediction_executable_cost import component as base_component

NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)

CHARGING_BPS = Decimal("500")
PER_LOT_FEE_UNITS = 12_000  # 0.05 x 400,000 x 600,000 / 1e6
FROZEN_COST_UNITS = 20 * (400_000 + PER_LOT_FEE_UNITS) + 20 * 400_000
PAYOUT_UNITS = 20_000_000
NET_PROFIT_UNITS = PAYOUT_UNITS - FROZEN_COST_UNITS  # 3,760,000


def _units_problem():
    """Exactly-one-YES two-leg problem at the #117 scale (1M units/$)."""
    base = base_component().problem
    observation: SettlementObservationKey = base.actions[
        0
    ].settlement_observation_key
    atom_kind = base.terminal_state_sets[0].atoms[0].kind

    def _action(action_id: str, contract_id: str, venue: str, side: ActionSide):
        return CandidateAction(
            action_id,
            venue,
            f"account-{action_id[-1]}",
            "chain-a",
            contract_id,
            observation,
            side,
            1,
            1,
            20,
            20,
            "usd-cents",
            "usd-cents",
            "usd-cents-v1",
            (ExecutableCostSlice(1, 20, 400_000),),
        )

    actions = (
        _action("action-a", "contract-a", "venue-a", ActionSide.BUY_YES),
        _action("action-b", "contract-b", "venue-b", ActionSide.BUY_NO),
    )
    winning_a = TerminalAtom(
        "contract-a-yes",
        atom_kind,
        "rules-v1",
        (ActionPayout("action-a", 1_000_000),),
        NOW + timedelta(days=20),
    )
    winning_b = TerminalAtom(
        "contract-b-yes",
        atom_kind,
        "rules-v1",
        (ActionPayout("action-b", 1_000_000),),
        NOW + timedelta(days=20),
    )
    states = (
        TerminalStateSet("contract-a", observation, "rules-v1", (winning_a,)),
        TerminalStateSet("contract-b", observation, "rules-v1", (winning_b,)),
    )
    return replace(
        base,
        actions=actions,
        terminal_state_sets=states,
    )


def _frozen(bounded_cost_units: int = FROZEN_COST_UNITS) -> dict[str, object]:
    problem = _units_problem()
    quantities = (
        (ActionQuantity("action-a", 20), ActionQuantity("action-b", 20)),
    )
    return {
        "component_id": "component:contract-a:contract-b",
        "market": {
            "problem": canonical_payload(problem),
            "quantities": [
                {"action_id": "action-a", "quantity_lots": 20},
                {"action_id": "action-b", "quantity_lots": 20},
            ],
            "bounded_cost_units": bounded_cost_units,
            "bounded_payout_units": PAYOUT_UNITS,
            "guaranteed_profit_units": PAYOUT_UNITS - bounded_cost_units,
            "capital_release_at": (NOW + timedelta(days=20)).isoformat(),
        },
        "execution": {
            "capital_use_units": FROZEN_COST_UNITS,
            "quantities": [
                {"action_id": "action-a", "quantity_lots": 20},
                {"action_id": "action-b", "quantity_lots": 20},
            ],
        },
        "fee": {
            "status": "fee_charging",
            "modeled": True,
            "taker_fee_rate_bps": 500,
            "taker_fee_units": 20 * PER_LOT_FEE_UNITS,
        },
    }


def _fresh_leg(
    action_id: str,
    *,
    price: str = "0.40",
    bps: Decimal | None,
    received_at: datetime | None = NOW,
    exchange_time: datetime | None = NOW,
    sequence: int | None = 1,
    size: str = "20",
) -> SnapshotLeg:
    level = (BookLevel(Decimal(price), Decimal(size)),)
    if action_id == "action-b":
        book = LegBook(bids=level, asks=(), taker_fee_bps=bps, available=True)
    else:
        book = LegBook(bids=(), asks=level, taker_fee_bps=bps, available=True)
    return SnapshotLeg(
        action_id,
        book,
        received_at,
        exchange_time,
        sequence,
    )


def _fresh_books(**overrides) -> ComponentSnapshot:
    """Fresh books identical to the frozen economics: 0.40 both legs, one
    leg charging 500 bps."""
    kwargs = dict(
        received_at_a=NOW,
        received_at_b=NOW,
        exchange_time_a=NOW,
        exchange_time_b=NOW,
        sequence_a=1,
        sequence_b=2,
        price_a="0.40",
        price_b="0.40",
        bps_a=CHARGING_BPS,
        bps_b=Decimal("0"),
    )
    kwargs.update(overrides)
    return ComponentSnapshot(
        "component:contract-a:contract-b",
        (
            _fresh_leg(
                "action-a",
                price=kwargs["price_a"],
                bps=kwargs["bps_a"],
                received_at=kwargs["received_at_a"],
                exchange_time=kwargs["exchange_time_a"],
                sequence=kwargs["sequence_a"],
            ),
            _fresh_leg(
                "action-b",
                price=kwargs["price_b"],
                bps=kwargs["bps_b"],
                received_at=kwargs["received_at_b"],
                exchange_time=kwargs["exchange_time_b"],
                sequence=kwargs["sequence_b"],
            ),
        ),
    )


SAFETY = {
    "max_quote_age_seconds": 10,
    "max_cross_leg_skew_seconds": 5,
    "max_per_trade_cost_units": 1_000_000_000,
}

POLICY = {
    "min_profit_usd": "0.002",
    "min_net_margin": "0.01",
    "min_annualized_return": "0.15",
    "max_capital_release_days": 30,
}


def test_c1_identical_fresh_books_pass_with_hand_worked_net_profit() -> None:
    result = preflight(
        _frozen(), _fresh_books(), SAFETY, POLICY, now=NOW
    )

    assert result["ok"] is True
    assert result["reason"] == "PASS"
    assert result["fresh_cost_units"] == FROZEN_COST_UNITS
    assert result["net_profit_units"] == NET_PROFIT_UNITS
    assert NET_PROFIT_UNITS == 3_760_000
    checks = {check["key"]: check for check in result["checks"]}
    assert checks["depth"]["passed"] is True
    assert checks["qualification"]["passed"] is True
    assert checks["price_bounds"]["passed"] is True


def test_c2_stale_quote_skew_and_missing_sequence_fail_closed() -> None:
    frozen = _frozen()

    stale = preflight(
        frozen,
        _fresh_books(received_at_a=NOW - timedelta(seconds=11)),
        SAFETY,
        POLICY,
        now=NOW,
    )
    assert stale["ok"] is False
    assert stale["reason"] == "QUOTE_STALE"

    skew = preflight(
        frozen,
        _fresh_books(exchange_time_b=NOW - timedelta(seconds=6)),
        SAFETY,
        POLICY,
        now=NOW,
    )
    assert skew["ok"] is False
    assert skew["reason"] == "CROSS_LEG_SKEW"

    missing = preflight(
        frozen,
        _fresh_books(sequence_b=None),
        SAFETY,
        POLICY,
        now=NOW,
    )
    assert missing["ok"] is False
    assert missing["reason"] == "SEQUENCE_MISSING"


def test_c3_price_worse_within_proven_bound_passes_beyond_fails() -> None:
    # Leg repriced to 0.42: fresh cost rises by 20 x 20,000 + fee, still
    # within the proven bound when the bound leaves headroom.
    frozen_headroom = _frozen(bounded_cost_units=17_000_000)
    within = preflight(
        frozen_headroom,
        _fresh_books(price_a="0.42"),
        SAFETY,
        POLICY,
        now=NOW,
    )
    assert within["ok"] is True
    assert within["reason"] == "PRICE_WITHIN_BOUNDS"

    beyond = preflight(
        _frozen(),
        _fresh_books(price_a="0.42"),
        SAFETY,
        POLICY,
        now=NOW,
    )
    assert beyond["ok"] is False
    assert beyond["reason"] == "PRICE_BEYOND_BOUND"


# Review round 2 (ruling 7): a frozen sequence baseline turns the sequence
# check into a real monotonicity gate — a fresh sequence BEHIND the baseline
# is SEQUENCE_REGRESSED; equal or newer passes.


def test_p3_sequence_behind_frozen_baseline_is_regressed() -> None:
    frozen = _frozen()
    frozen["sequences"] = {"action-a": 5, "action-b": 6}

    regressed = preflight(
        frozen,
        _fresh_books(sequence_a=3, sequence_b=6),
        SAFETY,
        POLICY,
        now=NOW,
    )
    assert regressed["ok"] is False
    assert regressed["reason"] == "SEQUENCE_REGRESSED"

    equal_or_newer = preflight(
        frozen,
        _fresh_books(sequence_a=5, sequence_b=7),
        SAFETY,
        POLICY,
        now=NOW,
    )
    assert equal_or_newer["ok"] is True
    assert equal_or_newer["reason"] == "PASS"
