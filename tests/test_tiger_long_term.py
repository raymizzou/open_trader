from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from open_trader.standard_strategies import StrategyBar
from open_trader.tiger_long_term import (
    allocate_target_weights,
    load_tiger_long_term_config,
    rebalance_reasons,
    sma200_state,
)


def bars(closes: list[str]) -> list[StrategyBar]:
    start = date(2020, 1, 1)
    return [
        StrategyBar(
            start + timedelta(days=index),
            Decimal(close),
            Decimal(close),
            Decimal(close),
            Decimal(close),
            Decimal("100"),
        )
        for index, close in enumerate(closes)
    ]


def test_loads_fixed_tiger_pool(tmp_path) -> None:
    path = tmp_path / "strategy.json"
    path.write_text(
        '{"strategy_id":"tiger_sma200_equal_weight/v1",'
        '"account_alias":"tiger_5683",'
        '"members":{"QQQ":"broad_us_growth"}}',
        encoding="utf-8",
    )

    config = load_tiger_long_term_config(path)

    assert config.members == {"QQQ": "broad_us_growth"}


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"strategy_id":"wrong","account_alias":"tiger_5683","members":{"QQQ":"broad"}}',
        '{"strategy_id":"tiger_sma200_equal_weight/v1","account_alias":"","members":{"QQQ":"broad"}}',
        '{"strategy_id":"tiger_sma200_equal_weight/v1","account_alias":"tiger_5683","members":{}}',
    ],
)
def test_rejects_invalid_tiger_pool(tmp_path, payload: str) -> None:
    path = tmp_path / "strategy.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="Tiger 长线策略配置无效"):
        load_tiger_long_term_config(path)


def test_sma200_uses_only_completed_closes() -> None:
    assert sma200_state(bars(["100"] * 199)) == "INELIGIBLE"
    assert sma200_state(bars(["100"] * 200 + ["101"])) == "LONG"
    assert sma200_state(bars(["100"] * 200 + ["100"])) == "CASH"


def test_allocation_scales_concentrated_risk_group() -> None:
    semiconductor = ["DRAM", "SOXX", "EUV", "TSM"]
    states = {symbol: "LONG" for symbol in [*semiconductor, "QQQ"]}
    groups = {symbol: "semiconductor" for symbol in semiconductor} | {
        "QQQ": "broad"
    }

    weights = allocate_target_weights(states, groups)

    assert sum((weights[symbol] for symbol in semiconductor), Decimal("0")) == Decimal(
        "0.30"
    )
    assert weights["QQQ"] == Decimal("0.10")


def test_allocation_equal_weights_more_than_ten_long_members() -> None:
    states = {f"S{index}": "LONG" for index in range(20)}
    groups = {symbol: symbol for symbol in states}

    weights = allocate_target_weights(states, groups)

    assert set(weights.values()) == {Decimal("0.05")}
    assert sum(weights.values(), Decimal("0")) == Decimal("1")


def test_rebalance_ignores_two_point_drift_but_reports_larger_drift() -> None:
    assert rebalance_reasons(
        {"QQQ": Decimal("0.08")},
        {"QQQ": Decimal("0.10")},
        {"QQQ": "LONG"},
        {"QQQ": "LONG"},
        {"QQQ": "broad"},
    ) == {}
    assert rebalance_reasons(
        {"QQQ": Decimal("0.079")},
        {"QQQ": Decimal("0.10")},
        {"QQQ": "LONG"},
        {"QQQ": "LONG"},
        {"QQQ": "broad"},
    ) == {"QQQ": "drift"}


def test_rebalance_prioritizes_state_and_hard_caps() -> None:
    assert rebalance_reasons(
        {"QQQ": Decimal("0.10")},
        {"QQQ": Decimal("0")},
        {"QQQ": "LONG"},
        {"QQQ": "CASH"},
        {"QQQ": "broad"},
    ) == {"QQQ": "state_change"}
    assert rebalance_reasons(
        {"QQQ": Decimal("0.101")},
        {"QQQ": Decimal("0.10")},
        {"QQQ": "LONG"},
        {"QQQ": "LONG"},
        {"QQQ": "broad"},
    ) == {"QQQ": "symbol_cap"}
    assert rebalance_reasons(
        {symbol: Decimal("0.08") for symbol in ["DRAM", "SOXX", "EUV", "TSM"]},
        {symbol: Decimal("0.075") for symbol in ["DRAM", "SOXX", "EUV", "TSM"]},
        {symbol: "LONG" for symbol in ["DRAM", "SOXX", "EUV", "TSM"]},
        {symbol: "LONG" for symbol in ["DRAM", "SOXX", "EUV", "TSM"]},
        {
            symbol: "semiconductor"
            for symbol in ["DRAM", "SOXX", "EUV", "TSM"]
        },
    ) == {
        symbol: "risk_group_cap"
        for symbol in ["DRAM", "SOXX", "EUV", "TSM"]
    }
