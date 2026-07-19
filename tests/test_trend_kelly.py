from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from open_trader.trend_api_stats import (
    build_trend_api_stats_payload,
    write_trend_api_stats,
)
from open_trader.trend_kelly import (
    TREND_API_STATS_SCHEMA_VERSION,
    TrendKellyRound,
    calculate_trend_kelly,
    load_trend_kelly_rounds,
    maximize_average_log_growth,
    trend_kelly_rounds_from_payload,
)


def _round(
    index: int,
    net_return: str,
    *,
    market: str = "US",
    strategy_id: str | None = None,
    version: str = "v3",
) -> TrendKellyRound:
    return TrendKellyRound(
        round_id=f"round-{index:03d}",
        source="simulation",
        market=market,
        strategy_id=strategy_id or f"trend_animals_warm_to_hot/{market}/v3",
        opening_strategy_version=version,
        closed_at=f"2026-01-{index // 24 + 1:02d}T{index % 24:02d}:00:00+00:00",
        net_return=Decimal(net_return),
        costs_complete=True,
        attribution_status="attributed",
        kelly_eligible=True,
    )


@pytest.mark.parametrize(
    ("returns", "expected"),
    [
        (("0.10", "0.20"), Decimal("1")),
        (("0.10", "-0.10"), Decimal("0")),
        (("0.10", "-1"), Decimal("0")),
        (("0", "0"), Decimal("0")),
    ],
)
def test_log_growth_optimizer_has_deterministic_boundaries(
    returns: tuple[str, ...], expected: Decimal,
) -> None:
    assert maximize_average_log_growth(tuple(map(Decimal, returns))) == expected


def test_log_growth_optimizer_is_order_invariant_and_quantizes_down() -> None:
    returns = (Decimal("0.8"), Decimal("-0.5"), Decimal("0.2"))

    forward = maximize_average_log_growth(returns)
    reverse = maximize_average_log_growth(tuple(reversed(returns)))

    assert forward == reverse == Decimal("0.605776")


@pytest.mark.parametrize(
    ("count", "phase", "selected", "enabled"),
    [
        (29, "cold_start", 29, False),
        (30, "active_all_samples", 30, True),
        (199, "active_all_samples", 199, True),
        (200, "active_rolling_200", 200, True),
    ],
)
def test_kelly_sample_boundaries(
    count: int, phase: str, selected: int, enabled: bool,
) -> None:
    state = calculate_trend_kelly(
        [_round(index, "0.10") for index in range(count)],
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v3",
        opening_strategy_version="v3",
    )

    assert state.phase == phase
    assert state.eligible_sample_count == count
    assert state.selected_sample_count == selected
    assert state.enabled is enabled
    assert state.quarter_kelly_cap == (Decimal("0.25") if enabled else None)


def test_kelly_uses_latest_200_by_close_identity_not_input_order() -> None:
    rounds = [_round(index, "0.10") for index in range(201)]
    rounds[0] = _round(0, "-1")

    state = calculate_trend_kelly(
        list(reversed(rounds)),
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v3",
        opening_strategy_version="v3",
    )

    assert state.eligible_sample_count == 201
    assert state.selected_round_ids == tuple(
        f"round-{index:03d}" for index in range(1, 201)
    )
    assert state.quarter_kelly_cap == Decimal("0.25")


def test_kelly_rolling_order_compares_mixed_offsets_as_instants() -> None:
    rounds = [
        replace(
            _round(index, "0.10"),
            closed_at=f"2026-02-{index // 24 + 1:02d}T{index % 24:02d}:00:00+00:00",
        )
        for index in range(201)
    ]
    rounds[0] = replace(rounds[0], closed_at="2026-01-01T23:30:00-05:00")
    rounds[1] = replace(rounds[1], closed_at="2026-01-02T01:00:00+00:00")

    state = calculate_trend_kelly(
        rounds,
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v3",
        opening_strategy_version="v3",
    )

    assert "round-000" in state.selected_round_ids
    assert "round-001" not in state.selected_round_ids


def test_kelly_rejects_returns_below_unlevered_total_loss() -> None:
    rounds = [_round(index, "0.10") for index in range(29)]
    rounds.append(_round(29, "-1.000001"))

    with pytest.raises(
        ValueError,
        match=r"^Kelly round round-029 net_return must be at least -1$",
    ):
        calculate_trend_kelly(
            rounds,
            market="US",
            strategy_id="trend_animals_warm_to_hot/US/v3",
            opening_strategy_version="v3",
        )


def test_kelly_rejects_noncanonical_or_naive_close_timestamp() -> None:
    rounds = [replace(_round(0, "0.10"), closed_at="2026-01-02T01:00:00")]

    with pytest.raises(
        ValueError,
        match=r"^Kelly round round-000 closed_at must be canonical timezone-aware ISO$",
    ):
        calculate_trend_kelly(
            rounds,
            market="US",
            strategy_id="trend_animals_warm_to_hot/US/v3",
            opening_strategy_version="v3",
        )


@pytest.mark.parametrize(
    ("market", "other_market"),
    [("CN", "HK"), ("HK", "US"), ("US", "CN")],
)
def test_kelly_isolates_market_strategy_version_and_actual_account(
    market: str, other_market: str,
) -> None:
    strategy_id = f"trend_animals_warm_to_hot/{market}/v3"
    matching = [_round(index, "0.10", market=market) for index in range(30)]
    noise = [
        _round(30, "-1", market=other_market),
        _round(31, "-1", market=market, strategy_id="another-strategy"),
        _round(32, "-1", market=market, version="v2"),
        TrendKellyRound(
            **{
                **_round(33, "-1", market=market).__dict__,
                "source": "actual",
            }
        ),
    ]

    state = calculate_trend_kelly(
        [*matching, *noise],
        market=market,
        strategy_id=strategy_id,
        opening_strategy_version="v3",
    )

    assert state.eligible_sample_count == 30
    assert state.quarter_kelly_cap == Decimal("0.25")


def test_kelly_cap_can_decline_recover_and_reach_zero() -> None:
    declined_rounds = [
        *(_round(index, "0.10") for index in range(15)),
        *(_round(index, "-0.099") for index in range(15, 30)),
    ]
    recovered_rounds = [
        *declined_rounds,
        *(_round(index, "0.10") for index in range(30, 60)),
    ]
    zero_rounds = [
        *(_round(index, "0.10") for index in range(15)),
        *(_round(index, "-0.10") for index in range(15, 30)),
    ]

    declined = calculate_trend_kelly(
        declined_rounds,
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v3",
        opening_strategy_version="v3",
    )
    recovered = calculate_trend_kelly(
        recovered_rounds,
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v3",
        opening_strategy_version="v3",
    )
    zero = calculate_trend_kelly(
        zero_rounds,
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v3",
        opening_strategy_version="v3",
    )

    assert Decimal("0") < declined.quarter_kelly_cap < Decimal("0.04")
    assert recovered.quarter_kelly_cap > declined.quarter_kelly_cap
    assert zero.quarter_kelly_cap == Decimal("0")
    assert zero.reason == "quarter Kelly cap is zero; future entries paused"


def _stats_round(index: int, net_return: object = "0.10") -> dict[str, object]:
    return {
        "round_id": f"round-{index:03d}",
        "source": "simulation",
        "market": "US",
        "strategy_id": "trend_animals_warm_to_hot/US/v3",
        "opening_strategy_version": "v3",
        "closed_at": f"2026-01-02T{index % 24:02d}:00:00+00:00",
        "net_return": net_return,
        "costs_complete": True,
        "attribution_status": "attributed",
        "kelly_eligible": True,
    }


def test_stats_adapter_ignores_actual_open_and_ineligible_rows() -> None:
    eligible = _stats_round(1)
    actual = {**_stats_round(2, "not-a-number"), "source": "actual"}
    ineligible = {**_stats_round(3, "not-a-number"), "kelly_eligible": False}
    payload = {
        "schema_version": TREND_API_STATS_SCHEMA_VERSION,
        "rounds": [actual, eligible, ineligible],
        "open_rounds": [{"source": "simulation", "unrealized_return": "999"}],
        "actual_account": {"unrealized_pnl": "999999"},
    }

    assert trend_kelly_rounds_from_payload(payload) == (
        TrendKellyRound(
            round_id="round-001",
            source="simulation",
            market="US",
            strategy_id="trend_animals_warm_to_hot/US/v3",
            opening_strategy_version="v3",
            closed_at="2026-01-02T01:00:00+00:00",
            net_return=Decimal("0.10"),
            costs_complete=True,
            attribution_status="attributed",
            kelly_eligible=True,
        ),
    )


def test_kelly_sample_updates_only_after_a_simulation_round_closes() -> None:
    open_round = {
        "round_id": "round-001",
        "source": "simulation",
        "market": "US",
        "strategy_id": "trend_animals_warm_to_hot/US/v3",
        "opening_strategy_version": "v3",
        "unrealized_return": "0.10",
    }
    before_close = trend_kelly_rounds_from_payload(
        {
            "schema_version": TREND_API_STATS_SCHEMA_VERSION,
            "rounds": [],
            "open_rounds": [open_round],
        }
    )
    after_close = trend_kelly_rounds_from_payload(
        {
            "schema_version": TREND_API_STATS_SCHEMA_VERSION,
            "rounds": [_stats_round(1)],
            "open_rounds": [],
        }
    )

    assert before_close == ()
    assert tuple(item.round_id for item in after_close) == ("round-001",)


def test_stats_loader_treats_missing_artifact_as_cold_start(tmp_path) -> None:
    assert load_trend_kelly_rounds(tmp_path / "data") == ()


def test_stats_loader_accepts_derived_rounds_and_rejects_tampering(tmp_path) -> None:
    data_dir = tmp_path / "data"
    common = {
        "source": "simulation",
        "source_id": "simulation:futu:102",
        "broker": "futu",
        "account_id": "102",
        "market": "US",
        "symbol": "AAA",
        "currency": "USD",
        "quantity": "1",
        "fee": "0",
        "costs_complete": True,
        "strategy_id": "trend_animals_warm_to_hot/US/v4",
        "strategy_version": "v4",
        "normal_cost_rate": "0.001",
        "normal_cost_model": "预计完整开平仓正常成本按名义金额计提",
        "report_sha256": "a" * 64,
        "attribution_status": "attributed",
        "exclusion_reason": "",
    }
    payload = build_trend_api_stats_payload(
        [
            {
                **common,
                "fill_id": "buy-1",
                "order_id": "buy-order-1",
                "side": "buy",
                "price": "100",
                "filled_at": "2026-06-01T10:00:00+00:00",
            },
            {
                **common,
                "fill_id": "sell-1",
                "order_id": "sell-order-1",
                "side": "sell",
                "price": "110.11",
                "filled_at": "2026-06-01T11:00:00+00:00",
            },
        ],
        strategy_versions=[
            {
                "market": "US",
                "strategy_id": "trend_animals_warm_to_hot/US/v4",
                "strategy_version": "v4",
            }
        ],
        generated_at="2026-06-02T00:00:00+00:00",
        statistics_cutoff_at="2026-06-01T23:59:59+00:00",
    )
    path = write_trend_api_stats(data_dir, payload)

    rounds = load_trend_kelly_rounds(data_dir)

    assert len(rounds) == 1
    assert rounds[0].net_return == Decimal("0.1")

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["rounds"][0]["net_return"] = "999"
    path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"trend_api_stats\.json validation failed: rounds are not derived",
    ):
        load_trend_kelly_rounds(data_dir)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": TREND_API_STATS_SCHEMA_VERSION, "rounds": {}},
        {
            "schema_version": TREND_API_STATS_SCHEMA_VERSION,
            "rounds": [_stats_round(1, "NaN")],
        },
    ],
)
def test_stats_adapter_fails_closed_on_malformed_simulation_evidence(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="trend_api_stats.json"):
        trend_kelly_rounds_from_payload(payload)
