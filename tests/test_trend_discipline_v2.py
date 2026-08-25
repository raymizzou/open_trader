from decimal import Decimal

import pytest

from open_trader.a_share_trend import (
    CandidateInput,
    HoldingSnapshot,
    build_candidate_list,
    live_trend_strategy_snapshot,
    normalize_trend_strategy_snapshot,
    plan_rotation_pairs_with_comparisons,
    freeze_allocation_reference,
    valid_frozen_report_contract,
)
from open_trader.trend_allocation import build_allocation_snapshot
from open_trader.trend_animals import TrendAnimalsError


def _roots() -> dict[str, object]:
    return {
        "CN": {
            "stock": {"asset": "A股", "tm_id": 1, "as_of_date": "2026-08-20", "global_strength": "62"},
            "etf": {"asset": "ETF基金", "tm_id": 2, "as_of_date": "2026-08-20", "global_strength": "99"},
        },
        "HK": {
            "stock": {"asset": "港股", "tm_id": 3, "as_of_date": "2026-08-20", "global_strength": "78"},
            "etf": {"asset": "香港ETF", "tm_id": 4, "as_of_date": "2026-08-20", "global_strength": "75"},
        },
        "US": {
            "stock": {"asset": "美股", "tm_id": 5, "as_of_date": "2026-08-20", "global_strength": "80"},
            "etf": {"asset": "美国ETF", "tm_id": 6, "as_of_date": "2026-08-20", "global_strength": "95"},
        },
    }


def test_v2_allocation_ranks_stock_roots_and_assigns_dynamic_slots() -> None:
    snapshot = build_allocation_snapshot(
        allocation_date="2026-08-20",
        generated_at="2026-08-20T16:20:00+08:00",
        git_sha="a" * 40,
        roots=_roots(),
        previous=None,
        version=2,
    )

    assert snapshot["version"] == 2
    assert snapshot["generator_version"] == "trend-allocation-v2"
    assert list(snapshot["markets"]) == ["US", "HK", "CN"]
    assert snapshot["markets"] == {
        "US": {"rank": 1, "score": "80", "score_source": "美股", "entry_weight": "0.04", "nominal_weight": "0.80", "position_limit": 20},
        "HK": {"rank": 2, "score": "78", "score_source": "港股", "entry_weight": "0.04", "nominal_weight": "0.60", "position_limit": 15},
        "CN": {"rank": 3, "score": "62", "score_source": "A股", "entry_weight": "0.04", "nominal_weight": "0.40", "position_limit": 10},
    }


def test_v2_allocation_roundtrip_selects_current_strategy_identity_and_limit() -> None:
    snapshot = build_allocation_snapshot(
        allocation_date="2026-08-20",
        generated_at="2026-08-20T16:20:00+08:00",
        git_sha="a" * 40,
        roots=_roots(),
        previous=None,
        version=2,
    )
    from open_trader.a_share_trend import live_trend_strategy_snapshot

    strategy = live_trend_strategy_snapshot(
        "US",
        "process",
        (622460, 705013),
        allocation={
            "daily_path": "data/trend_allocation/daily/2026-08-20.json",
            "sha256": "b" * 64,
            "snapshot": snapshot,
            "reused": False,
            "stale_a_trading_days": 0,
            "failure_reason": "",
        },
    )

    assert strategy["strategy_version"] == "v14"
    assert strategy["parameters"]["allocation_position_limit"] == 20
    assert strategy["parameters"]["target_weight"] == "0.04"


@pytest.mark.parametrize(("market", "strategy_version"), [("CN", "v15"), ("HK", "v13"), ("US", "v13")])
def test_current_v2_strategy_snapshots_normalize_with_allocation_facts(
    market: str, strategy_version: str,
) -> None:
    allocation_snapshot = build_allocation_snapshot(
        allocation_date="2026-08-20",
        generated_at="2026-08-20T16:20:00+08:00",
        git_sha="a" * 40,
        roots=_roots(),
        previous=None,
        version=2,
    )
    strategy = live_trend_strategy_snapshot(
        market,
        "process",
        (622460, 705013),
        strategy_version=strategy_version,
        allocation={
            "daily_path": "data/trend_allocation/daily/2026-08-20.json",
            "sha256": "b" * 64,
            "snapshot": allocation_snapshot,
        },
    )

    assert normalize_trend_strategy_snapshot(strategy, market) == strategy


@pytest.mark.parametrize("market", ["CN", "HK", "US"])
def test_strategy_identity_rejects_both_allocation_version_hybrids(
    market: str,
) -> None:
    v1 = build_allocation_snapshot(
        allocation_date="2026-08-20",
        generated_at="2026-08-20T16:20:00+08:00",
        git_sha="a" * 40,
        roots=_roots(),
        previous=None,
        version=1,
    )
    v2 = build_allocation_snapshot(
        allocation_date="2026-08-20",
        generated_at="2026-08-20T16:20:00+08:00",
        git_sha="a" * 40,
        roots=_roots(),
        previous=None,
        version=2,
    )

    with pytest.raises(ValueError, match="allocation"):
        live_trend_strategy_snapshot(
            market,
            "process",
            (),
            strategy_version={"CN": "v15", "HK": "v13", "US": "v13"}[market],
            allocation={
                "daily_path": "data/trend_allocation/daily/2026-08-20.json",
                "sha256": "b" * 64,
                "snapshot": v1,
            },
        )

    with pytest.raises(ValueError, match="allocation"):
        live_trend_strategy_snapshot(
            market,
            "process",
            (),
            strategy_version={"CN": "v14", "HK": "v12", "US": "v12"}[market],
            allocation={
                "daily_path": "data/trend_allocation/daily/2026-08-20.json",
                "sha256": "b" * 64,
                "snapshot": v2,
            },
        )


def test_v2_allocation_reuses_previous_order_for_ties_and_date_mismatch() -> None:
    previous = build_allocation_snapshot(
        allocation_date="2026-08-19",
        generated_at="2026-08-19T16:20:00+08:00",
        git_sha="a" * 40,
        roots=_roots(),
        previous=None,
        version=2,
    )
    roots = _roots()
    roots["CN"]["stock"]["global_strength"] = "80"
    roots["HK"]["stock"]["global_strength"] = "80"
    roots["US"]["stock"]["as_of_date"] = "2026-08-19"
    current = build_allocation_snapshot(
        allocation_date="2026-08-20",
        generated_at="2026-08-20T16:20:00+08:00",
        git_sha="a" * 40,
        roots=roots,
        previous=previous,
        version=2,
    )

    assert list(current["markets"]) == list(previous["markets"])


def test_v2_pure_tie_rejects_v1_predecessor() -> None:
    previous = build_allocation_snapshot(
        allocation_date="2026-08-19",
        generated_at="2026-08-19T16:20:00+08:00",
        git_sha="a" * 40,
        roots=_roots(),
        previous=None,
        version=1,
    )
    roots = _roots()
    roots["CN"]["stock"]["global_strength"] = "80"
    roots["HK"]["stock"]["global_strength"] = "80"

    with pytest.raises(TrendAnimalsError, match="v2"):
        build_allocation_snapshot(
            allocation_date="2026-08-20",
            generated_at="2026-08-20T16:20:00+08:00",
            git_sha="a" * 40,
            roots=roots,
            previous=previous,
            version=2,
        )


def test_v2_allocation_reuses_previous_snapshot_when_strength_is_missing() -> None:
    previous = build_allocation_snapshot(
        allocation_date="2026-08-19",
        generated_at="2026-08-19T16:20:00+08:00",
        git_sha="a" * 40,
        roots=_roots(),
        previous=None,
        version=2,
    )
    roots = _roots()
    roots["CN"]["stock"].pop("global_strength")

    reused = build_allocation_snapshot(
        allocation_date="2026-08-20",
        generated_at="2026-08-20T16:20:00+08:00",
        git_sha="b" * 40,
        roots=roots,
        previous=previous,
        version=2,
    )

    assert reused == previous
    with pytest.raises(TrendAnimalsError, match="previous snapshot"):
        build_allocation_snapshot(
            allocation_date="2026-08-20",
            generated_at="2026-08-20T16:20:00+08:00",
            git_sha="b" * 40,
            roots=roots,
            previous=None,
            version=2,
        )


def test_v2_allocation_reuses_previous_snapshot_when_stock_root_is_missing() -> None:
    previous = build_allocation_snapshot(
        allocation_date="2026-08-19",
        generated_at="2026-08-19T16:20:00+08:00",
        git_sha="a" * 40,
        roots=_roots(),
        previous=None,
        version=2,
    )
    roots = _roots()
    roots["CN"].pop("stock")

    reused = build_allocation_snapshot(
        allocation_date="2026-08-20",
        generated_at="2026-08-20T16:20:00+08:00",
        git_sha="b" * 40,
        roots=roots,
        previous=previous,
        version=2,
    )

    assert reused == previous
    with pytest.raises(TrendAnimalsError, match="previous snapshot"):
        build_allocation_snapshot(
            allocation_date="2026-08-20",
            generated_at="2026-08-20T16:20:00+08:00",
            git_sha="b" * 40,
            roots=roots,
            previous=None,
            version=2,
        )


def test_v2_frozen_report_contract_requires_dynamic_position_limit() -> None:
    from open_trader.a_share_trend import live_trend_strategy_snapshot

    snapshot = build_allocation_snapshot(
        allocation_date="2026-08-20",
        generated_at="2026-08-20T16:20:00+08:00",
        git_sha="a" * 40,
        roots=_roots(),
        previous=None,
        version=2,
    )
    reference = {
        "daily_path": "data/trend_allocation/daily/2026-08-20.json",
        "sha256": "b" * 64,
        "snapshot": snapshot,
        "reused": False,
        "stale_a_trading_days": 0,
        "failure_reason": "",
    }
    strategy = live_trend_strategy_snapshot(
        "US", "process", (622460, 705013), allocation=reference,
    )
    frozen = freeze_allocation_reference(reference)
    assert frozen is not None
    payload = {
        "execution_date": "2026-08-20",
        "metadata": {"market": "US"},
        "allocation": frozen,
        "strategy_snapshot": strategy,
        "strategy_judgments": {
            "holding_decisions": [],
            "top10_candidates": [],
            "simulate_rotation_pairs": [],
            "real_rotation_pairs": [],
            "simulate_rotation_comparisons": [],
            "real_rotation_comparisons": [],
        },
    }

    assert valid_frozen_report_contract(payload)
    tampered = {
        **payload,
        "strategy_snapshot": {
            **strategy,
            "parameters": {
                **strategy["parameters"],
                "allocation_position_limit": 15,
            },
        },
    }
    assert not valid_frozen_report_contract(tampered)


def _candidate(symbol: str, strength: str, *, temperature: str = "热") -> CandidateInput:
    return CandidateInput(
        tm_id=hash(symbol) % 100000,
        symbol=symbol,
        exchange="SH",
        name=symbol,
        asset="A股",
        industry="行业",
        industry_tm_id=1,
        as_of_date="2026-08-20",
        tradable=True,
        amount=Decimal("3"),
        right_side=True,
        days=2,
        strength=Decimal(strength),
        danger=False,
        close=Decimal("10"),
        atr=Decimal("1"),
        industry_temperature=temperature,
        temperature_prev="温",
        temperature_curr="热",
        phase="立夏",
        market_cap=Decimal("200"),
        global_strength=Decimal(strength),
    )


def test_current_versions_compete_candidates_by_individual_global_strength() -> None:
    result = build_candidate_list(
        [_candidate("C", "95"), _candidate("A", "95"), _candidate("B", "95", temperature="沸")],
        held_symbols=set(),
        expected_date="2026-08-20",
        market="CN",
        strategy_version="v15",
    )

    assert [item.symbol for item in result.eligible] == ["B", "A", "C"]


def _holding(symbol: str, strength: str) -> HoldingSnapshot:
    return HoldingSnapshot(
        tm_id=hash(symbol) % 100000,
        symbol=symbol,
        exchange="SH",
        name=symbol,
        as_of_date="2026-08-20",
        right_side=True,
        danger=False,
        boiling=False,
        champagne=False,
        asset="A股",
        strength=Decimal(strength),
        global_strength=Decimal(strength),
    )


def test_rotation_uses_global_strength_and_plans_all_unique_pairs() -> None:
    pairs, comparisons = plan_rotation_pairs_with_comparisons(
        holdings=[_holding("600001", "40"), _holding("600002", "45"), _holding("600003", "50")],
        candidates=[_candidate("600101", "80"), _candidate("600102", "75"), _candidate("600103", "70")],
        entry_weight=Decimal("0.04"),
        available_slots=0,
        pair_slots=(0, 1, 2),
        market="CN",
        use_global_strength=True,
    )

    assert len(pairs) == 3
    assert len(comparisons) == 3
    assert all(item.strength_basis == "global" for item in pairs)
