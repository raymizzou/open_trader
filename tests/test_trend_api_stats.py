from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, getcontext
import hashlib

import pytest

from open_trader.trend_api_stats import (
    build_statement_actual_stats_payload,
    build_trend_api_stats_payload,
    eligible_simulation_rounds,
    load_trend_api_stats,
    read_trend_api_stats_snapshot,
    trend_statistics_disposition,
    strategy_payoff_ratio,
    write_trend_api_stats,
)
from open_trader.trend_kelly import calculate_trend_kelly, trend_kelly_rounds_from_payload


def fill(
    fill_id: str,
    *,
    side: str,
    quantity: str,
    price: str,
    fee: str,
    filled_at: str,
    strategy_id: str = "trend_animals_warm_to_hot/US/v1",
    strategy_version: str = "v1",
    source: str = "simulation",
    broker: str = "futu",
    account_id: str = "101",
    attribution_status: str = "attributed",
    exclusion_reason: str = "",
    market: str = "US",
    currency: str = "USD",
) -> dict[str, object]:
    result = {
        "fill_id": fill_id,
        "order_id": f"order-{fill_id}",
        "source": source,
        "source_id": f"{source}:{broker}:{account_id}",
        "broker": broker,
        "account_id": account_id,
        "market": market,
        "symbol": "AAA",
        "currency": currency,
        "side": side,
        "quantity": quantity,
        "price": price,
        "fee": fee,
        "costs_complete": True,
        "filled_at": filled_at,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "attribution_status": attribution_status,
        "exclusion_reason": exclusion_reason,
    }
    if source == "simulation" and attribution_status == "attributed":
        result.update({
            "normal_cost_rate": "0.001",
            "normal_cost_model": "预计完整开平仓正常成本按名义金额计提",
            "report_sha256": "a" * 64,
        })
    return result


def _cn_closed_pair(
    label: str, version: str, *, source: str,
) -> list[dict[str, object]]:
    broker = "futu" if source == "simulation" else "eastmoney"
    account_id = "12958918" if source == "simulation" else "eastmoney_main"
    pair = [
        fill(
            f"{label}-buy",
            side="buy",
            quantity="1",
            price="10",
            fee="0.1",
            filled_at="2026-07-10T15:00:00+08:00",
            strategy_id=f"trend_animals_warm_to_hot/CN/{version}",
            strategy_version=version,
            source=source,
            broker=broker,
            account_id=account_id,
            market="CN",
            currency="CNY",
        ),
        fill(
            f"{label}-sell",
            side="sell",
            quantity="1",
            price="11",
            fee="0.1",
            filled_at="2026-07-11T15:00:00+08:00",
            strategy_id=f"trend_animals_warm_to_hot/CN/{version}",
            strategy_version=version,
            source=source,
            broker=broker,
            account_id=account_id,
            market="CN",
            currency="CNY",
        ),
    ]
    for sequence, item in enumerate(pair, start=1):
        item["symbol"] = label
        if source == "actual":
            item.update({
                "statement_period": f"2026-07-{9 + sequence:02d}",
                "execution_granularity": "statement_trade_date",
                "timestamp_semantics": "market_close_ordering_sentinel",
                "statement_sequence": 1,
            })
    return pair


def _phillips_closed_pair(label: str, version: str) -> list[dict[str, object]]:
    return [{
        **item,
        "source_id": "actual:phillips:phillips_main",
        "broker": "phillips",
        "account_id": "phillips_main",
        "market": "HK",
        "currency": "HKD",
        "strategy_id": f"trend_animals_warm_to_hot/HK/{version}",
        "filled_at": item["filled_at"].replace("15:00:00", "16:00:00"),
    } for item in _cn_closed_pair(label, version, source="actual")]


def test_eastmoney_rebuild_changes_only_cn_actual_statement_period(
    tmp_path: Path,
) -> None:
    simulated = _cn_closed_pair("simulated", "v8", source="simulation")
    eastmoney = _cn_closed_pair("eastmoney", "v8", source="actual")
    phillips = _phillips_closed_pair("phillips", "v8")
    for item in [*eastmoney, *phillips]:
        item["statement_period"] = "2026-07"
    before = build_trend_api_stats_payload(
        [*simulated, *eastmoney, *phillips],
        strategy_versions=[
            {"market": "CN", "strategy_id": "trend_animals_warm_to_hot/CN/v8", "strategy_version": "v8"},
            {"market": "HK", "strategy_id": "trend_animals_warm_to_hot/HK/v8", "strategy_version": "v8"},
        ],
        generated_at="2026-08-08T18:00:00+08:00",
        statistics_cutoff_at="2026-08-08T16:00:00+08:00",
    )
    write_trend_api_stats(tmp_path / "data", before)
    replacement = _cn_closed_pair("eastmoney-replacement", "v8", source="actual")
    for item in replacement:
        item["statement_period"] = "2026-07"

    payload = build_statement_actual_stats_payload(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        broker="eastmoney",
        statement_period="2026-07",
        fills=replacement,
        generated_at="2026-08-08T18:00:00+08:00",
        statistics_cutoff_at="2026-07-31T15:00:00+08:00",
    )

    assert [item for item in payload["fills"] if item["broker"] == "phillips"] == [
        item for item in before["fills"] if item["broker"] == "phillips"
    ]
    assert [item for item in payload["fills"] if item["broker"] == "futu"] == [
        item for item in before["fills"] if item["broker"] == "futu"
    ]
    assert [item["fill_id"] for item in payload["fills"] if item["broker"] == "eastmoney"] == [
        item["fill_id"] for item in replacement
    ]


def test_phillips_rebuild_changes_only_hk_actual_statement_period(
    tmp_path: Path,
) -> None:
    simulated = _cn_closed_pair("simulated", "v8", source="simulation")
    eastmoney = _cn_closed_pair("eastmoney", "v8", source="actual")
    phillips = _phillips_closed_pair("phillips", "v8")
    for item in [*eastmoney, *phillips]:
        item["statement_period"] = "2026-07"
    before = build_trend_api_stats_payload(
        [*simulated, *eastmoney, *phillips],
        strategy_versions=[
            {"market": "CN", "strategy_id": "trend_animals_warm_to_hot/CN/v8", "strategy_version": "v8"},
            {"market": "HK", "strategy_id": "trend_animals_warm_to_hot/HK/v8", "strategy_version": "v8"},
        ],
        generated_at="2026-08-08T18:00:00+08:00",
        statistics_cutoff_at="2026-08-08T16:00:00+08:00",
    )
    write_trend_api_stats(tmp_path / "data", before)
    replacement = _phillips_closed_pair("phillips-replacement", "v8")
    for item in replacement:
        item["statement_period"] = "2026-07"

    payload = build_statement_actual_stats_payload(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        broker="phillips",
        statement_period="2026-07",
        fills=replacement,
        generated_at="2026-08-08T18:00:00+08:00",
        statistics_cutoff_at="2026-07-31T16:00:00+08:00",
    )

    assert [item for item in payload["fills"] if item["broker"] == "eastmoney"] == [
        item for item in before["fills"] if item["broker"] == "eastmoney"
    ]
    assert [item for item in payload["fills"] if item["broker"] == "futu"] == [
        item for item in before["fills"] if item["broker"] == "futu"
    ]
    assert [item["fill_id"] for item in payload["fills"] if item["broker"] == "phillips"] == [
        item["fill_id"] for item in replacement
    ]


@pytest.mark.parametrize(
    ("broker", "market", "currency", "account_id"),
    [
        ("eastmoney", "CN", "CNY", "eastmoney_main"),
        ("phillips", "HK", "HKD", "phillips_main"),
    ],
)
def test_statement_actual_fill_source_pairs_are_strictly_supported(
    broker: str,
    market: str,
    currency: str,
    account_id: str,
) -> None:
    actual = fill(
        "statement:2026-07:trade-1",
        side="buy",
        quantity="1",
        price="10",
        fee="0.1",
        filled_at="2026-07-10T10:00:00+08:00",
        source="actual",
        broker=broker,
        account_id=account_id,
        market=market,
        currency=currency,
    )
    actual["statement_period"] = "2026-07"
    actual["execution_granularity"] = "statement_trade_date"
    actual["timestamp_semantics"] = "market_close_ordering_sentinel"
    actual["statement_sequence"] = 1
    actual["filled_at"] = (
        "2026-07-10T15:00:00+08:00"
        if broker == "eastmoney"
        else "2026-07-10T16:00:00+08:00"
    )

    payload = build_trend_api_stats_payload(
        [actual],
        strategy_versions=[],
        generated_at="2026-07-11T00:00:00+08:00",
        statistics_cutoff_at="2026-07-10T23:59:59+08:00",
    )

    assert payload["fills"] == [actual]


def test_statement_actual_fill_rejects_wrong_broker_market_pair() -> None:
    actual = fill(
        "statement:2026-07:trade-1",
        side="buy",
        quantity="1",
        price="10",
        fee="0.1",
        filled_at="2026-07-10T10:00:00+08:00",
        source="actual",
        broker="eastmoney",
        account_id="eastmoney_main",
        market="HK",
        currency="HKD",
    )
    actual["statement_period"] = "2026-07"
    actual["execution_granularity"] = "statement_trade_date"
    actual["timestamp_semantics"] = "market_close_ordering_sentinel"
    actual["statement_sequence"] = 1

    with pytest.raises(ValueError, match="fill source"):
        build_trend_api_stats_payload(
            [actual],
            strategy_versions=[],
            generated_at="2026-07-11T00:00:00+08:00",
            statistics_cutoff_at="2026-07-10T23:59:59+08:00",
        )


def test_statement_fills_sort_by_source_sequence_without_changing_api_order() -> None:
    statement_fills = [
        fill(
            fill_id,
            side="buy",
            quantity="1",
            price="10",
            fee="0.1",
            filled_at="2026-07-10T15:00:00+08:00",
            source="actual",
            broker="eastmoney",
            account_id="eastmoney_main",
            market="CN",
            currency="CNY",
        )
        for fill_id in ("z-source-first", "a-source-second")
    ]
    for sequence, statement_fill in enumerate(statement_fills, start=1):
        statement_fill.update({
            "statement_period": "2026-07",
            "execution_granularity": "statement_trade_date",
            "timestamp_semantics": "market_close_ordering_sentinel",
            "statement_sequence": sequence,
        })
    closing_fill = fill(
        "m-close",
        side="sell",
        quantity="2",
        price="11",
        fee="0.2",
        filled_at="2026-07-11T15:00:00+08:00",
        source="actual",
        broker="eastmoney",
        account_id="eastmoney_main",
        market="CN",
        currency="CNY",
    )
    closing_fill.update({
        "statement_period": "2026-07",
        "execution_granularity": "statement_trade_date",
        "timestamp_semantics": "market_close_ordering_sentinel",
        "statement_sequence": 3,
    })
    statement_fills.append(closing_fill)
    api_fills = [
        fill(
            fill_id,
            side="buy",
            quantity="1",
            price="10",
            fee="0.1",
            filled_at="2026-07-10T07:00:00+00:00",
            source="actual",
            broker="tiger",
            account_id="U1",
        )
        for fill_id in ("z-api", "a-api")
    ]

    payload = build_trend_api_stats_payload(
        [*statement_fills, *api_fills],
        strategy_versions=[],
        generated_at="2026-07-12T00:00:00+08:00",
        statistics_cutoff_at="2026-07-11T23:59:59+08:00",
    )

    assert [
        item["fill_id"]
        for item in payload["fills"]
        if item["broker"] == "eastmoney"
    ] == ["z-source-first", "a-source-second", "m-close"]
    assert [
        item["fill_id"]
        for item in payload["fills"]
        if item["broker"] == "tiger"
    ] == ["a-api", "z-api"]
    statement_round = next(
        round_ for round_ in payload["rounds"] if round_["broker"] == "eastmoney"
    )
    assert statement_round["opening_fill_id"] == "z-source-first"
    assert statement_round["fill_ids"] == [
        "z-source-first",
        "a-source-second",
        "m-close",
    ]


def test_closed_round_deduplicates_fills_and_aggregates_scaled_entry_partial_exit_and_fees() -> None:
    fills = [
        fill("b1", side="buy", quantity="10", price="10", fee="1", filled_at="2026-01-01T10:00:00+00:00", source="actual", broker="tiger", account_id="U1"),
        fill("b2", side="buy", quantity="5", price="12", fee="0.5", filled_at="2026-01-02T10:00:00+00:00", source="actual", broker="tiger", account_id="U1"),
        fill("s1", side="sell", quantity="4", price="15", fee="0.4", filled_at="2026-01-03T10:00:00+00:00", source="actual", broker="tiger", account_id="U1"),
        fill("s2", side="sell", quantity="11", price="14", fee="1.1", filled_at="2026-01-04T10:00:00+00:00", source="actual", broker="tiger", account_id="U1"),
    ]

    partial = build_trend_api_stats_payload(
        fills[:-1] + [deepcopy(fills[0])],
        strategy_versions=[{
            "market": "US",
            "strategy_id": "trend_animals_warm_to_hot/US/v1",
            "strategy_version": "v1",
        }],
        generated_at="2026-01-05T00:00:00+00:00",
        statistics_cutoff_at="2026-01-04T10:00:00+00:00",
    )
    closed = build_trend_api_stats_payload(
        fills + [deepcopy(fills[0]), deepcopy(fills[2])],
        strategy_versions=[{
            "market": "US",
            "strategy_id": "trend_animals_warm_to_hot/US/v1",
            "strategy_version": "v1",
        }],
        generated_at="2026-01-05T00:00:00+00:00",
        statistics_cutoff_at="2026-01-04T10:00:00+00:00",
    )

    assert partial["rounds"] == []
    assert len(partial["fills"]) == 3
    assert len(closed["fills"]) == 4
    assert closed["rounds"] == [{
        "round_id": closed["rounds"][0]["round_id"],
        "source": "actual",
        "source_id": "actual:tiger:U1",
        "broker": "tiger",
        "account_id": "U1",
        "market": "US",
        "symbol": "AAA",
        "currency": "USD",
        "strategy_id": "trend_animals_warm_to_hot/US/v1",
        "opening_strategy_version": "v1",
        "opened_at": "2026-01-01T10:00:00+00:00",
        "closed_at": "2026-01-04T10:00:00+00:00",
        "opening_fill_id": "b1",
        "fill_ids": ["b1", "b2", "s1", "s2"],
        "buy_quantity": "15",
        "sell_quantity": "15",
        "buy_notional": "160",
        "sell_notional": "214",
        "fees": "3",
        "costs_complete": True,
        "cost_source": "broker_actual",
        "normal_cost_rate": None,
        "normal_cost_model": None,
        "opening_report_sha256": None,
        "net_pnl": "51",
        "net_return": "0.3157894736842105263157894737",
        "result": "win",
        "attribution_status": "attributed",
        "exclusion_reason": "",
        "kelly_eligible": False,
    }]
    actual = next(stat for stat in closed["stats"] if stat["source"] == "actual")
    assert actual["eligible_sample_count"] == 1


def test_conflicting_duplicate_fill_identity_fails_closed() -> None:
    original = fill(
        "b1", side="buy", quantity="10", price="10", fee="1",
        filled_at="2026-01-01T10:00:00+00:00",
    )
    conflicting = {**original, "price": "11"}

    with pytest.raises(ValueError, match="conflicting duplicate fill"):
        build_trend_api_stats_payload(
            [original, conflicting],
            strategy_versions=[],
            generated_at="2026-01-05T00:00:00+00:00",
            statistics_cutoff_at="2026-01-04T10:00:00+00:00",
        )


def test_round_keeps_opening_version_and_new_version_has_empty_actual_stats() -> None:
    buy = fill(
        "actual-buy", side="buy", quantity="2", price="10", fee="0.1",
        filled_at="2026-01-01T10:00:00+00:00",
        source="actual", broker="tiger", account_id="U1",
    )
    sell = fill(
        "actual-sell", side="sell", quantity="2", price="12", fee="0.1",
        filled_at="2026-02-01T10:00:00+00:00",
        strategy_id="trend_animals_warm_to_hot/US/v2",
        strategy_version="v2",
        source="actual", broker="tiger", account_id="U1",
    )

    payload = build_trend_api_stats_payload(
        [buy, sell],
        strategy_versions=[
            {"market": "US", "strategy_id": "trend_animals_warm_to_hot/US/v1", "strategy_version": "v1"},
            {"market": "US", "strategy_id": "trend_animals_warm_to_hot/US/v2", "strategy_version": "v2"},
        ],
        generated_at="2026-02-02T00:00:00+00:00",
        statistics_cutoff_at="2026-02-01T10:00:00+00:00",
    )

    assert payload["rounds"][0]["strategy_id"] == "trend_animals_warm_to_hot/US/v1"
    assert payload["rounds"][0]["opening_strategy_version"] == "v1"
    assert payload["rounds"][0]["kelly_eligible"] is False
    actual = {
        (stat["strategy_id"], stat["opening_strategy_version"]): stat
        for stat in payload["stats"]
        if stat["source"] == "actual"
    }
    assert actual[("trend_animals_warm_to_hot/US/v1", "v1")]["eligible_sample_count"] == 1
    assert actual[("trend_animals_warm_to_hot/US/v2", "v2")]["eligible_sample_count"] == 0
    assert actual[("trend_animals_warm_to_hot/US/v2", "v2")]["win_rate"] is None


def test_cn_v7_stats_inherit_only_v4_for_both_sources() -> None:
    fills = [
        item
        for source in ("simulation", "actual")
        for version in ("v4", "v5", "v6", "v7")
        for item in _cn_closed_pair(
            f"{source}-{version}", version, source=source
        )
    ]
    payload = build_trend_api_stats_payload(
        fills,
        strategy_versions=[{
            "market": "CN",
            "strategy_id": "trend_animals_warm_to_hot/CN/v7",
            "strategy_version": "v7",
        }],
        generated_at="2026-07-12T00:00:00+08:00",
        statistics_cutoff_at="2026-07-11T23:59:59+08:00",
    )
    stats = {
        (
            item["source"],
            item["strategy_id"],
            item["opening_strategy_version"],
        ): item
        for item in payload["stats"]
    }

    for source in ("simulation", "actual"):
        assert stats[
            (source, "trend_animals_warm_to_hot/CN/v7", "v7")
        ]["eligible_sample_count"] == 2
        assert stats[
            (source, "trend_animals_warm_to_hot/CN/v4", "v4")
        ]["eligible_sample_count"] == 1


def test_cn_v8_stats_expose_all_approved_contributing_opening_versions() -> None:
    fills = [
        item
        for source in ("simulation", "actual")
        for version in ("v4", "v5", "v6", "v7", "v8")
        for item in _cn_closed_pair(
            f"{source}-{version}", version, source=source
        )
    ]
    payload = build_trend_api_stats_payload(
        fills,
        strategy_versions=[{
            "market": "CN",
            "strategy_id": "trend_animals_warm_to_hot/CN/v8",
            "strategy_version": "v8",
        }],
        generated_at="2026-07-12T00:00:00+08:00",
        statistics_cutoff_at="2026-07-11T23:59:59+08:00",
    )
    stats = {
        (
            item["source"],
            item["strategy_id"],
            item["opening_strategy_version"],
        ): item
        for item in payload["stats"]
    }

    for source in ("simulation", "actual"):
        assert stats[
            (source, "trend_animals_warm_to_hot/CN/v8", "v8")
        ]["eligible_sample_count"] == 3
        assert stats[
            (source, "trend_animals_warm_to_hot/CN/v5", "v5")
        ]["eligible_sample_count"] == 1
        assert stats[
            (source, "trend_animals_warm_to_hot/CN/v6", "v6")
        ]["eligible_sample_count"] == 1


def test_fill_and_round_order_uses_timestamp_instants_not_iso_text() -> None:
    buy = fill(
        "buy", side="buy", quantity="1", price="10", fee="0",
        filled_at="2026-01-01T09:00:00+08:00",
    )
    sell = fill(
        "sell", side="sell", quantity="1", price="11", fee="0",
        filled_at="2026-01-01T02:00:00+00:00",
    )

    payload = build_trend_api_stats_payload(
        [sell, buy],
        strategy_versions=[],
        generated_at="2026-01-02T00:00:00+00:00",
        statistics_cutoff_at="2026-01-01T02:00:00+00:00",
    )

    assert [item["fill_id"] for item in payload["fills"]] == ["buy", "sell"]
    assert payload["rounds"][0]["opened_at"] == "2026-01-01T09:00:00+08:00"
    assert payload["rounds"][0]["closed_at"] == "2026-01-01T02:00:00+00:00"


def test_conflicting_scaled_buy_attribution_excludes_entire_round() -> None:
    fills = [
        fill("buy-v1", side="buy", quantity="1", price="10", fee="0", filled_at="2026-01-01T10:00:00+00:00"),
        fill(
            "buy-unknown", side="buy", quantity="1", price="11", fee="0",
            filled_at="2026-01-02T10:00:00+00:00",
            strategy_id="", strategy_version="",
            attribution_status="ambiguous",
            exclusion_reason="multiple_opening_strategy_matches",
        ),
        fill("sell", side="sell", quantity="2", price="12", fee="0", filled_at="2026-01-03T10:00:00+00:00"),
    ]

    payload = build_trend_api_stats_payload(
        fills,
        strategy_versions=[{
            "market": "US", "strategy_id": "trend_animals_warm_to_hot/US/v1", "strategy_version": "v1",
        }],
        generated_at="2026-01-04T00:00:00+00:00",
        statistics_cutoff_at="2026-01-03T10:00:00+00:00",
    )

    round_ = payload["rounds"][0]
    assert round_["opening_strategy_version"] == "v1"
    assert round_["attribution_status"] == "ambiguous"
    assert round_["exclusion_reason"] == "scaled_entry_attribution_conflict"
    assert round_["kelly_eligible"] is False
    simulation = next(stat for stat in payload["stats"] if stat["source"] == "simulation")
    assert simulation["eligible_sample_count"] == 0


def test_round_fails_closed_when_linked_fill_source_facts_disagree() -> None:
    buy = fill(
        "buy", side="buy", quantity="1", price="10", fee="0",
        filled_at="2026-01-01T10:00:00+00:00",
    )
    sell = fill(
        "sell", side="sell", quantity="1", price="11", fee="0",
        filled_at="2026-01-02T10:00:00+00:00",
    )
    sell["currency"] = "HKD"

    with pytest.raises(ValueError, match="round fills disagree on currency"):
        build_trend_api_stats_payload(
            [buy, sell],
            strategy_versions=[],
            generated_at="2026-01-03T00:00:00+00:00",
            statistics_cutoff_at="2026-01-02T10:00:00+00:00",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("broker", "tiger"),
        ("source_id", "simulation:futu:other"),
        ("market", "XX"),
    ],
)
def test_fill_source_identity_facts_are_structurally_consistent(
    field: str, value: str,
) -> None:
    broker_fill = fill(
        "buy", side="buy", quantity="1", price="10", fee="0",
        filled_at="2026-01-01T10:00:00+00:00",
    )
    broker_fill[field] = value

    with pytest.raises(ValueError, match="fill source"):
        build_trend_api_stats_payload(
            [broker_fill],
            strategy_versions=[],
            generated_at="2026-01-02T00:00:00+00:00",
            statistics_cutoff_at="2026-01-01T10:00:00+00:00",
        )


def test_derived_decimals_ignore_the_process_global_decimal_context() -> None:
    fills = [
        fill(
            "buy", side="buy", quantity="3", price="1", fee="0",
            filled_at="2026-01-01T10:00:00+00:00",
            source="actual", broker="tiger", account_id="U1",
        ),
        fill(
            "sell", side="sell", quantity="3",
            price="1.333333333333333333333333333333", fee="0",
            filled_at="2026-01-02T10:00:00+00:00",
            source="actual", broker="tiger", account_id="U1",
        ),
    ]
    original_precision = getcontext().prec
    try:
        getcontext().prec = 6
        low_precision = build_trend_api_stats_payload(
            fills,
            strategy_versions=[],
            generated_at="2026-01-03T00:00:00+00:00",
            statistics_cutoff_at="2026-01-02T10:00:00+00:00",
        )
        getcontext().prec = 50
        high_precision = build_trend_api_stats_payload(
            fills,
            strategy_versions=[],
            generated_at="2026-01-03T00:00:00+00:00",
            statistics_cutoff_at="2026-01-02T10:00:00+00:00",
        )
    finally:
        getcontext().prec = original_precision

    assert low_precision == high_precision


def test_simulation_uses_only_opening_frozen_cost_model_and_missing_model_is_ineligible() -> None:
    buy = fill(
        "buy", side="buy", quantity="10", price="10", fee="0",
        filled_at="2026-01-01T10:00:00+00:00",
    )
    sell = fill(
        "sell", side="sell", quantity="10", price="11", fee="0",
        filled_at="2026-01-02T10:00:00+00:00",
    )
    complete = build_trend_api_stats_payload(
        [buy, sell],
        strategy_versions=[],
        generated_at="2026-01-03T00:00:00+00:00",
        statistics_cutoff_at="2026-01-02T10:00:00+00:00",
    )["rounds"][0]
    incomplete_buy = deepcopy(buy)
    for key in ("normal_cost_rate", "normal_cost_model", "report_sha256"):
        incomplete_buy.pop(key)
    incomplete = build_trend_api_stats_payload(
        [incomplete_buy, sell],
        strategy_versions=[],
        generated_at="2026-01-03T00:00:00+00:00",
        statistics_cutoff_at="2026-01-02T10:00:00+00:00",
    )["rounds"][0]

    assert complete["fees"] == "0.1"
    assert complete["costs_complete"] is True
    assert complete["cost_source"] == "opening_strategy_normal_cost_model"
    assert complete["normal_cost_rate"] == "0.001"
    assert complete["normal_cost_model"] == "预计完整开平仓正常成本按名义金额计提"
    assert complete["opening_report_sha256"] == "a" * 64
    assert complete["net_pnl"] == "9.9"
    assert complete["net_return"] == "0.0989010989010989010989010989"
    assert complete["kelly_eligible"] is True
    assert incomplete["costs_complete"] is False
    assert incomplete["net_pnl"] is None
    assert incomplete["net_return"] is None
    assert incomplete["kelly_eligible"] is False


def test_payoff_ratio_uses_average_round_returns_and_reports_edge_states() -> None:
    returns = ("0.2", "0.1", "-0.1", "-0.2")
    fills: list[dict[str, object]] = []
    for index, net_return in enumerate(returns, start=1):
        exit_price = str(Decimal("1") + Decimal(net_return))
        fills.extend([
            fill(
                f"b{index}", side="buy", quantity="1", price="1", fee="0",
                filled_at=f"2026-01-{index * 2 - 1:02d}T10:00:00+00:00",
                source="actual", broker="tiger", account_id="U1",
            ),
            fill(
                f"s{index}", side="sell", quantity="1", price=exit_price, fee="0",
                filled_at=f"2026-01-{index * 2:02d}T10:00:00+00:00",
                source="actual", broker="tiger", account_id="U1",
            ),
        ])

    payload = build_trend_api_stats_payload(
        fills,
        strategy_versions=[],
        generated_at="2026-01-09T00:00:00+00:00",
        statistics_cutoff_at="2026-01-08T10:00:00+00:00",
    )
    actual = next(stat for stat in payload["stats"] if stat["source"] == "actual")

    assert actual["eligible_sample_count"] == 4
    assert actual["win_rate"] == "0.5"
    assert actual["payoff_ratio"] == "1"
    assert actual["payoff_ratio_status"] == "available"
    assert strategy_payoff_ratio([], ["-0.1"]) == (None, "no_wins")
    assert strategy_payoff_ratio(["0.1"], []) == (None, "no_losses")
    assert strategy_payoff_ratio(["0.1"], ["0"]) == (None, "zero_denominator")


def test_eligible_simulation_adapter_revalidates_derivation_and_isolates_actual() -> None:
    fills = [
        fill("sim-buy", side="buy", quantity="1", price="10", fee="0", filled_at="2026-01-01T10:00:00+00:00"),
        fill("sim-sell", side="sell", quantity="1", price="12", fee="0", filled_at="2026-01-02T10:00:00+00:00"),
        fill("actual-buy", side="buy", quantity="1", price="10", fee="0.1", filled_at="2026-01-01T10:00:00+00:00", source="actual", broker="tiger", account_id="U1"),
        fill("actual-sell", side="sell", quantity="1", price="20", fee="0.1", filled_at="2026-01-02T10:00:00+00:00", source="actual", broker="tiger", account_id="U1"),
    ]
    payload = build_trend_api_stats_payload(
        fills,
        strategy_versions=[],
        generated_at="2026-01-03T00:00:00+00:00",
        statistics_cutoff_at="2026-01-02T10:00:00+00:00",
    )

    eligible = eligible_simulation_rounds(
        payload,
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v1",
        opening_strategy_version="v1",
    )

    assert sorted(round_["source"] for round_ in payload["rounds"]) == ["actual", "simulation"]
    assert [round_["source"] for round_ in eligible] == ["simulation"]
    assert eligible[0]["net_return"] != next(
        round_["net_return"] for round_ in payload["rounds"] if round_["source"] == "actual"
    )
    tampered = deepcopy(payload)
    tampered["stats"][0]["eligible_sample_count"] = 999
    with pytest.raises(ValueError, match="stats are not derived from rounds"):
        eligible_simulation_rounds(
            tampered,
            market="US",
            strategy_id="trend_animals_warm_to_hot/US/v1",
        )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("outside_strategy", "no_matching_opening_strategy_action"),
        ("ambiguous", "multiple_opening_strategy_matches"),
    ],
)
def test_non_strategy_actual_rounds_are_explicit_and_excluded(
    status: str, reason: str,
) -> None:
    fills = [
        fill(
            "buy", side="buy", quantity="1", price="10", fee="0.1",
            filled_at="2026-01-01T10:00:00+00:00",
            strategy_id="", strategy_version="", source="actual",
            broker="tiger", account_id="U1", attribution_status=status,
            exclusion_reason=reason,
        ),
        fill(
            "sell", side="sell", quantity="1", price="11", fee="0.1",
            filled_at="2026-01-02T10:00:00+00:00",
            strategy_id="", strategy_version="", source="actual",
            broker="tiger", account_id="U1", attribution_status=status,
            exclusion_reason=reason,
        ),
    ]
    payload = build_trend_api_stats_payload(
        fills,
        strategy_versions=[{
            "market": "US", "strategy_id": "trend_animals_warm_to_hot/US/v1", "strategy_version": "v1",
        }],
        generated_at="2026-01-03T00:00:00+00:00",
        statistics_cutoff_at="2026-01-02T10:00:00+00:00",
    )

    assert payload["rounds"][0]["attribution_status"] == status
    assert payload["rounds"][0]["exclusion_reason"] == reason
    assert next(stat for stat in payload["stats"] if stat["source"] == "actual")["eligible_sample_count"] == 0

    fills[0]["exclusion_reason"] = ""
    with pytest.raises(ValueError, match="non-attributed fill exclusion_reason is required"):
        build_trend_api_stats_payload(
            fills,
            strategy_versions=[],
            generated_at="2026-01-03T00:00:00+00:00",
            statistics_cutoff_at="2026-01-02T10:00:00+00:00",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", "actual"),
        ("broker", "tiger"),
        ("account_id", ""),
        ("market", "XX"),
        ("source_id", "simulation:futu:other"),
        ("orders_seen", -1),
        ("fill_count", True),
        ("status", "partial"),
        ("statistics_cutoff_at", "2026-01-03T00:00:00+00:00"),
    ],
)
def test_source_audit_records_are_strict_and_do_not_exceed_artifact_cutoff(
    field: str, value: object,
) -> None:
    payload = build_trend_api_stats_payload(
        [],
        strategy_versions=[],
        generated_at="2026-01-03T00:00:00+00:00",
        statistics_cutoff_at="2026-01-02T00:00:00+00:00",
    )
    source = {
        "source": "simulation",
        "source_id": "simulation:futu:101",
        "broker": "futu",
        "account_id": "101",
        "market": "US",
        "orders_seen": 2,
        "fill_count": 0,
        "statistics_cutoff_at": "2026-01-02T00:00:00+00:00",
        "status": "available",
    }
    payload["sources"] = [{**source, field: value}]

    with pytest.raises(ValueError, match="source"):
        eligible_simulation_rounds(
            payload,
            market="US",
            strategy_id="trend_animals_warm_to_hot/US/v1",
        )


def test_source_audit_cutoff_may_precede_shared_artifact_cutoff() -> None:
    payload = build_trend_api_stats_payload(
        [],
        strategy_versions=[],
        generated_at="2026-01-03T00:00:00+00:00",
        statistics_cutoff_at="2026-01-02T00:00:00+00:00",
    )
    payload["sources"] = [{
        "source": "actual",
        "source_id": "actual:eastmoney:eastmoney_main",
        "broker": "eastmoney",
        "account_id": "eastmoney_main",
        "market": "CN",
        "orders_seen": 0,
        "fill_count": 0,
        "statistics_cutoff_at": "2026-01-01T00:00:00+00:00",
        "status": "available",
    }]

    assert eligible_simulation_rounds(
        payload,
        market="CN",
        strategy_id="trend_animals_warm_to_hot/CN/v4",
    ) == []


@pytest.mark.parametrize(
    ("generated_at", "cutoff", "fill_time", "message"),
    [
        (
            "2026-01-01T23:59:59+00:00",
            "2026-01-02T00:00:00+00:00",
            None,
            "generated_at must not precede statistics_cutoff_at",
        ),
        (
            "2026-01-03T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
            "2026-01-02T00:00:01+00:00",
            "fill filled_at exceeds statistics_cutoff_at",
        ),
    ],
)
def test_artifact_cutoff_chronology_fails_closed(
    generated_at: str,
    cutoff: str,
    fill_time: str | None,
    message: str,
) -> None:
    fills = [] if fill_time is None else [fill(
        "late", side="buy", quantity="1", price="10", fee="0",
        filled_at=fill_time,
    )]

    with pytest.raises(ValueError, match=message):
        build_trend_api_stats_payload(
            fills,
            strategy_versions=[],
            generated_at=generated_at,
            statistics_cutoff_at=cutoff,
        )


def test_artifact_load_reports_missing_and_unreadable_files_as_validation_errors(
    tmp_path,
) -> None:
    with pytest.raises(ValueError, match="is missing"):
        load_trend_api_stats(tmp_path)
    path = tmp_path / "latest/trend_api_stats.json"
    path.parent.mkdir(parents=True)
    path.write_text("not JSON", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON"):
        load_trend_api_stats(tmp_path)


def test_artifact_loaders_reject_utf8_bom_as_invalid_json(tmp_path) -> None:
    path = tmp_path / "latest/trend_api_stats.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf{}")

    for loader in (load_trend_api_stats, read_trend_api_stats_snapshot):
        with pytest.raises(ValueError, match="trend_api_stats.json is invalid JSON"):
            loader(tmp_path)


def test_snapshot_loader_returns_hash_of_exact_bytes(tmp_path) -> None:
    payload = build_trend_api_stats_payload(
        [],
        strategy_versions=[],
        generated_at="2026-08-08T17:00:00-04:00",
        statistics_cutoff_at="2026-08-08T16:00:00-04:00",
    )
    path = write_trend_api_stats(tmp_path, payload)
    raw = path.read_bytes()

    loaded, digest = read_trend_api_stats_snapshot(tmp_path)

    assert loaded == payload
    assert digest == hashlib.sha256(raw).hexdigest()


def test_statistics_disposition_conserves_every_candidate() -> None:
    actual = {
        "source": "actual", "broker": "tiger", "account_id": "U1",
        "strategy_id": "trend_animals_warm_to_hot/US/v8",
        "strategy_version": "v8",
    }
    payload = build_trend_api_stats_payload(
        [
            fill("eligible-buy", side="buy", quantity="1", price="10", fee="0.1", filled_at="2026-08-04T10:00:00-04:00", **actual),
            fill("eligible-sell", side="sell", quantity="1", price="12", fee="0.1", filled_at="2026-08-05T10:00:00-04:00", **actual),
            fill("manual-buy", side="buy", quantity="1", price="10", fee="0.1", filled_at="2026-08-06T10:00:00-04:00", source="actual", broker="tiger", account_id="U1", strategy_id="", strategy_version="", attribution_status="outside_strategy", exclusion_reason="no_matching_opening_strategy_action"),
            fill("manual-sell", side="sell", quantity="1", price="11", fee="0.1", filled_at="2026-08-07T10:00:00-04:00", source="actual", broker="tiger", account_id="U1", strategy_id="", strategy_version="", attribution_status="outside_strategy", exclusion_reason="no_matching_opening_strategy_action"),
            fill("still-open", side="buy", quantity="1", price="9", fee="0.1", filled_at="2026-08-08T10:00:00-04:00", **actual),
        ],
        strategy_versions=[{"market": "US", "strategy_id": "trend_animals_warm_to_hot/US/v8", "strategy_version": "v8"}],
        generated_at="2026-08-08T17:00:00-04:00",
        statistics_cutoff_at="2026-08-08T16:00:00-04:00",
    )
    payload["sources"] = [{
        "source": "actual", "source_id": "actual:tiger:U1", "broker": "tiger",
        "account_id": "U1", "market": "US", "orders_seen": 5, "fill_count": 5,
        "statistics_cutoff_at": "2026-08-08T16:00:00-04:00", "status": "available",
    }]

    result = trend_statistics_disposition(payload, market="US", strategy_id="trend_animals_warm_to_hot/US/v8", opening_strategy_version="v8", source="actual")

    assert result["eligible_sample_count"] == 1
    assert result["excluded_candidate_count"] == 1
    assert result["incomplete_open_candidate_count"] == 1
    assert result["discovered_candidate_count"] == 3
    assert result["discovered_candidate_count"] == result["eligible_sample_count"] + result["excluded_candidate_count"] + result["incomplete_open_candidate_count"]
    assert result["exclusion_reasons"] == [{"reason": "no_matching_opening_strategy_action", "count": 1}]


def test_simulation_disposition_count_equals_calculate_trend_kelly_eligible_count() -> None:
    def pair(label: str, version: str, buy_at: str, sell_at: str) -> list[dict[str, object]]:
        entries = [
            fill(f"{label}-buy", side="buy", quantity="1", price="10", fee="0", filled_at=buy_at, strategy_id=f"trend_animals_warm_to_hot/US/{version}", strategy_version=version),
            fill(f"{label}-sell", side="sell", quantity="1", price="11", fee="0", filled_at=sell_at, strategy_id=f"trend_animals_warm_to_hot/US/{version}", strategy_version=version),
        ]
        for entry in entries:
            entry["symbol"] = label
        return entries

    payload = build_trend_api_stats_payload(
        [*pair("V4", "v4", "2026-08-04T10:00:00-04:00", "2026-08-05T10:00:00-04:00"), *pair("V8", "v8", "2026-08-06T10:00:00-04:00", "2026-08-07T10:00:00-04:00")],
        strategy_versions=[{"market": "US", "strategy_id": "trend_animals_warm_to_hot/US/v8", "strategy_version": "v8"}],
        generated_at="2026-08-08T17:00:00-04:00",
        statistics_cutoff_at="2026-08-08T16:00:00-04:00",
    )
    payload["sources"] = [{
        "source": "simulation", "source_id": "simulation:futu:101", "broker": "futu",
        "account_id": "101", "market": "US", "orders_seen": 4, "fill_count": 4,
        "statistics_cutoff_at": "2026-08-08T16:00:00-04:00", "status": "available",
    }]

    disposition = trend_statistics_disposition(payload, market="US", strategy_id="trend_animals_warm_to_hot/US/v8", opening_strategy_version="v8", source="simulation")

    assert disposition["eligible_sample_count"] == calculate_trend_kelly(
        trend_kelly_rounds_from_payload(payload), market="US", strategy_id="trend_animals_warm_to_hot/US/v8", opening_strategy_version="v8",
    ).eligible_sample_count
