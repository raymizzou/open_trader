from datetime import date, timedelta
from decimal import Decimal

import pytest

from open_trader.standard_strategies import (
    ACTION_TARGET_FRACTIONS,
    StrategyBar,
    generate_strategy_signals,
    strategy_catalog,
)


def test_strategy_catalog_has_three_fixed_v1_entries() -> None:
    assert [item.strategy_id for item in strategy_catalog()] == [
        "trend_pullback/v1",
        "breakout_momentum/v1",
        "range_mean_reversion/v1",
    ]
    assert [item.name_zh for item in strategy_catalog()] == [
        "趋势回调",
        "突破动量",
        "区间均值回归",
    ]


def test_action_target_fractions_are_stable() -> None:
    assert ACTION_TARGET_FRACTIONS == {
        "BUY": Decimal("0.5"),
        "ADD": Decimal("1"),
        "HOLD": None,
        "REDUCE": Decimal("0.5"),
        "EXIT": Decimal("0"),
    }


def _bar(day: date, close: str, *, low: str | None = None, volume: str = "100") -> StrategyBar:
    price = Decimal(close)
    return StrategyBar(
        date=day,
        open=price,
        high=price + Decimal("1"),
        low=Decimal(low) if low is not None else price - Decimal("1"),
        close=price,
        volume=Decimal(volume),
    )


def lifecycle_fixture(strategy_id: str) -> list[StrategyBar]:
    first = date(2025, 2, 1)
    if strategy_id == "trend_pullback/v1":
        warmup = [_bar(first + timedelta(days=i), str(80 + i // 3)) for i in range(59)]
        return [
            *warmup,
            _bar(date(2025, 4, 1), "100", low="94"),
            _bar(date(2025, 4, 2), "110"),
            _bar(date(2025, 4, 3), "130"),
            _bar(date(2025, 4, 4), "70"),
        ]
    if strategy_id == "breakout_momentum/v1":
        warmup = [
            _bar(first + timedelta(days=i), "108" if i >= 50 else "100")
            for i in range(59)
        ]
        return [
            *warmup,
            _bar(date(2025, 4, 1), "110", volume="200"),
            StrategyBar(date(2025, 4, 2), Decimal("110"), Decimal("150"), Decimal("80"), Decimal("120"), Decimal("100")),
            _bar(date(2025, 4, 3), "109.1"),
            _bar(date(2025, 4, 4), "108"),
        ]
    warmup = [_bar(first + timedelta(days=i), "100") for i in range(59)]
    return [
        *warmup,
        _bar(date(2025, 4, 1), "90"),
        _bar(date(2025, 4, 2), "96"),
        _bar(date(2025, 4, 3), "101"),
        _bar(date(2025, 4, 4), "115"),
    ]


def future_shock_bar() -> StrategyBar:
    return _bar(date(2025, 4, 5), "1", volume="1000000")


@pytest.mark.parametrize(
    ("strategy_id", "expected_actions"),
    [
        ("trend_pullback/v1", ["BUY", "ADD", "REDUCE", "EXIT"]),
        ("breakout_momentum/v1", ["BUY", "ADD", "REDUCE", "EXIT"]),
        ("range_mean_reversion/v1", ["BUY", "ADD", "REDUCE", "EXIT"]),
    ],
)
def test_strategy_fixture_covers_position_lifecycle(
    strategy_id: str, expected_actions: list[str]
) -> None:
    signals = generate_strategy_signals(
        strategy_id,
        lifecycle_fixture(strategy_id),
        start_date=date(2025, 4, 1),
        max_strategy_weight=Decimal("0.10"),
    )
    trades = [signal for signal in signals if signal.action != "HOLD"]
    assert [signal.action for signal in trades] == expected_actions
    assert [signal.target_weight for signal in trades] == [
        Decimal("0.05"), Decimal("0.10"), Decimal("0.05"), Decimal("0"),
    ]


def test_appending_future_bar_does_not_change_prior_decisions() -> None:
    bars = lifecycle_fixture("trend_pullback/v1")
    original = generate_strategy_signals(
        "trend_pullback/v1", bars, start_date=date(2025, 4, 1),
        max_strategy_weight=Decimal("0.10"),
    )
    extended = generate_strategy_signals(
        "trend_pullback/v1", [*bars, future_shock_bar()],
        start_date=date(2025, 4, 1), max_strategy_weight=Decimal("0.10"),
    )
    # Appending a bar necessarily changes only the old final row's execution
    # date from None to the appended date; every prior complete row is stable.
    assert extended[: len(original) - 1] == original[:-1]
    former_final = extended[len(original) - 1]
    assert (
        former_final.action,
        former_final.target_weight,
        former_final.rule,
        former_final.explanation,
        former_final.data_cutoff,
    ) == (
        original[-1].action,
        original[-1].target_weight,
        original[-1].rule,
        original[-1].explanation,
        original[-1].data_cutoff,
    )
    assert original[-1].earliest_execution_date is None
    assert former_final.earliest_execution_date == future_shock_bar().date


def test_warmup_bars_never_emit_trade_actions() -> None:
    start = date(2025, 4, 1)
    signals = generate_strategy_signals(
        "breakout_momentum/v1", lifecycle_fixture("breakout_momentum/v1"),
        start_date=start, max_strategy_weight=Decimal("0.10"),
    )
    assert all(signal.action == "HOLD" for signal in signals if signal.decision_date < start)


def test_breakout_uses_prior_session_high_not_prior_close() -> None:
    bars = lifecycle_fixture("breakout_momentum/v1")[:59]
    wick = bars[-1]
    bars[-1] = StrategyBar(wick.date, wick.open, Decimal("120"), wick.low, wick.close, wick.volume)
    bars.append(_bar(date(2025, 4, 1), "109", volume="200"))
    signals = generate_strategy_signals(
        "breakout_momentum/v1", bars, start_date=date(2025, 4, 1),
        max_strategy_weight=Decimal("0.10"),
    )
    assert signals[-1].action == "HOLD"


def test_buy_fills_at_next_open_before_next_close_is_evaluated() -> None:
    bars = lifecycle_fixture("breakout_momentum/v1")[:60]
    next_bar = StrategyBar(
        date=date(2025, 4, 2),
        open=Decimal("200"),
        high=Decimal("201"),
        low=Decimal("119"),
        close=Decimal("120"),
        volume=Decimal("100"),
    )
    signals = generate_strategy_signals(
        "breakout_momentum/v1", [*bars, next_bar],
        start_date=date(2025, 4, 1), max_strategy_weight=Decimal("0.10"),
    )
    assert signals[-2].action == "BUY"
    assert signals[-1].action == "EXIT"


@pytest.mark.parametrize(
    ("strategy_id", "maximum", "message"),
    [
        ("not-a-strategy", Decimal("0.10"), "未知策略"),
        ("trend_pullback/v1", Decimal("-0.01"), "最大策略权重不能为负数"),
    ],
)
def test_validation_errors_are_chinese(
    strategy_id: str, maximum: Decimal, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        generate_strategy_signals(
            strategy_id, [], start_date=date(2025, 4, 1),
            max_strategy_weight=maximum,
        )
