"""Versioned, deterministic standard strategy definitions and signals."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Mapping, Sequence


Action = Literal["BUY", "ADD", "HOLD", "REDUCE", "EXIT"]


@dataclass(frozen=True)
class StrategyBar:
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    name_zh: str
    description_zh: str
    parameters: Mapping[str, Decimal | int]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.strategy_id,
            "name_zh": self.name_zh,
            "description_zh": self.description_zh,
            "parameters": {key: str(value) for key, value in self.parameters.items()},
        }


@dataclass(frozen=True)
class StrategySignal:
    decision_date: date
    earliest_execution_date: date | None
    action: Action
    target_weight: Decimal | None
    rule: str
    explanation: str
    data_cutoff: date


ACTION_TARGET_FRACTIONS: dict[Action, Decimal | None] = {
    "BUY": Decimal("0.5"),
    "ADD": Decimal("1"),
    "HOLD": None,
    "REDUCE": Decimal("0.5"),
    "EXIT": Decimal("0"),
}

ACTION_PRECEDENCE = {"EXIT": 0, "REDUCE": 1, "ADD": 2, "BUY": 3, "HOLD": 4}

_CATALOG = (
    StrategyDefinition(
        "trend_pullback/v1", "趋势回调", "顺势回调至短期均线后买入。",
        {"sma_short": 20, "sma_long": 50, "atr_period": 14, "rsi_period": 14, "stop_multiplier": Decimal("2")},
    ),
    StrategyDefinition(
        "breakout_momentum/v1", "突破动量", "放量突破前期高点后跟随动量。",
        {"high_period": 20, "volume_period": 20, "volume_multiplier": Decimal("1.5"), "atr_period": 14, "sma_exit": 10, "stop_multiplier": Decimal("2")},
    ),
    StrategyDefinition(
        "range_mean_reversion/v1", "区间均值回归", "在区间下沿买入并等待均值回归。",
        {"bollinger_period": 20, "stddev_multiplier": Decimal("2"), "rsi_period": 14, "atr_period": 14, "stop_multiplier": Decimal("2")},
    ),
)


def strategy_catalog() -> tuple[StrategyDefinition, StrategyDefinition, StrategyDefinition]:
    return _CATALOG


def _sma(values: Sequence[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    return sum(values[-period:], Decimal("0")) / Decimal(period)


def _atr(bars: Sequence[StrategyBar], period: int) -> Decimal | None:
    if len(bars) < period + 1:
        return None
    ranges: list[Decimal] = []
    for previous, current in zip(bars[-period - 1 : -1], bars[-period:]):
        ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    return sum(ranges, Decimal("0")) / Decimal(period)


def _rsi(values: Sequence[Decimal], period: int) -> Decimal | None:
    if len(values) < period + 1:
        return None
    changes = [current - previous for previous, current in zip(values[-period - 1 : -1], values[-period:])]
    gain = sum((change for change in changes if change > 0), Decimal("0")) / Decimal(period)
    loss = sum((-change for change in changes if change < 0), Decimal("0")) / Decimal(period)
    if loss == 0:
        return Decimal("100") if gain else Decimal("50")
    return Decimal("100") - Decimal("100") / (Decimal("1") + gain / loss)


def _bollinger(values: Sequence[Decimal], period: int, multiplier: Decimal) -> tuple[Decimal, Decimal, Decimal] | None:
    middle = _sma(values, period)
    if middle is None:
        return None
    window = values[-period:]
    deviation = (sum(((value - middle) ** 2 for value in window), Decimal("0")) / Decimal(period)).sqrt()
    return middle - multiplier * deviation, middle, middle + multiplier * deviation


def _prior_high(values: Sequence[Decimal], period: int) -> Decimal | None:
    if len(values) <= period:
        return None
    return max(values[-period - 1 : -1])


def _target(action: Action, maximum: Decimal) -> Decimal | None:
    fraction = ACTION_TARGET_FRACTIONS[action]
    return None if fraction is None else maximum * fraction


def generate_strategy_signals(
    strategy_id: str,
    bars: Sequence[StrategyBar],
    *,
    start_date: date,
    max_strategy_weight: Decimal,
) -> list[StrategySignal]:
    if strategy_id not in {item.strategy_id for item in _CATALOG}:
        raise ValueError(f"未知策略：{strategy_id}")
    if max_strategy_weight < 0:
        raise ValueError("最大策略权重不能为负数")

    signals: list[StrategySignal] = []
    current_weight = Decimal("0")
    entry_price: Decimal | None = None
    active_stop: Decimal | None = None
    breakout_level: Decimal | None = None
    pending: tuple[Action, Decimal | None, Decimal | None] | None = None

    for index, bar in enumerate(bars):
        if pending is not None:
            pending_action, decision_atr, pending_breakout = pending
            if pending_action == "BUY":
                current_weight = max_strategy_weight * Decimal("0.5")
                entry_price = bar.open
                breakout_level = pending_breakout
                if decision_atr is not None:
                    active_stop = bar.open - Decimal("2") * decision_atr
            elif pending_action == "ADD":
                current_weight = max_strategy_weight
                if decision_atr is not None and strategy_id != "range_mean_reversion/v1":
                    active_stop = bar.open - Decimal("2") * decision_atr
            elif pending_action == "REDUCE":
                current_weight = max_strategy_weight * Decimal("0.5")
            elif pending_action == "EXIT":
                current_weight = Decimal("0")
                entry_price = active_stop = breakout_level = None
            pending = None

        history = bars[: index + 1]
        closes = [item.close for item in history]
        candidates: list[tuple[Action, str, str]] = [("HOLD", "hold", "未触发交易条件")]
        atr14 = _atr(history, 14)

        if bar.date >= start_date:
            if strategy_id == "trend_pullback/v1":
                sma20, sma50, rsi14 = _sma(closes, 20), _sma(closes, 50), _rsi(closes, 14)
                trend = sma20 is not None and sma50 is not None and sma20 > sma50 and bar.close > sma50
                if current_weight == 0 and trend and bar.low <= sma20 and bar.close > sma20:
                    candidates.append(("BUY", "trend_pullback_buy", "上升趋势中回调至 SMA20 后收复"))
                prior5 = _prior_high(closes, 5)
                if current_weight == max_strategy_weight * Decimal("0.5") and trend and prior5 is not None and bar.close > prior5:
                    candidates.append(("ADD", "trend_pullback_add", "趋势持续且突破前 5 日最高收盘价"))
                if current_weight == max_strategy_weight and sma20 is not None and atr14 is not None and (
                    (rsi14 is not None and rsi14 >= 75) or bar.close >= sma20 + Decimal("2") * atr14
                ):
                    candidates.append(("REDUCE", "trend_pullback_reduce", "趋势过热，减仓至初始目标"))
                if current_weight > 0 and sma50 is not None and (bar.close < sma50 or (active_stop is not None and bar.close < active_stop)):
                    candidates.append(("EXIT", "trend_pullback_exit", "跌破 SMA50 或活动止损"))

            elif strategy_id == "breakout_momentum/v1":
                prior20 = _prior_high([item.high for item in history], 20)
                prior_vol = _sma([item.volume for item in history[:-1]], 20)
                if current_weight == 0 and prior20 is not None and prior_vol is not None and bar.close > prior20 and bar.volume >= Decimal("1.5") * prior_vol:
                    candidates.append(("BUY", "breakout_buy", "放量突破前 20 日高点"))
                if current_weight == max_strategy_weight * Decimal("0.5") and entry_price is not None and atr14 is not None and breakout_level is not None and bar.close >= entry_price + atr14 and bar.close > breakout_level:
                    candidates.append(("ADD", "breakout_add", "突破后上涨至少一个 ATR"))
                sma10 = _sma(closes, 10)
                if current_weight == max_strategy_weight and sma10 is not None and breakout_level is not None and bar.close < sma10 and bar.close > breakout_level:
                    candidates.append(("REDUCE", "breakout_reduce", "跌破 SMA10 但仍守住突破位"))
                if current_weight > 0 and breakout_level is not None and (bar.close < breakout_level or (active_stop is not None and bar.close < active_stop)):
                    candidates.append(("EXIT", "breakout_exit", "跌破突破位或活动止损"))

            else:
                bands = _bollinger(closes, 20, Decimal("2"))
                rsi14 = _rsi(closes, 14)
                if bands is not None:
                    lower, middle, upper = bands
                    if current_weight == 0 and rsi14 is not None and bar.close <= lower and rsi14 <= 30:
                        candidates.append(("BUY", "range_buy", "触及布林带下轨且 RSI 超卖"))
                    if current_weight == max_strategy_weight * Decimal("0.5") and bar.close > lower and (active_stop is None or bar.close >= active_stop):
                        candidates.append(("ADD", "range_add", "价格重新收复布林带下轨"))
                    if current_weight == max_strategy_weight and bar.close >= middle:
                        candidates.append(("REDUCE", "range_reduce", "价格回归布林带中轨"))
                    if current_weight > 0 and (bar.close >= upper or (active_stop is not None and bar.close < active_stop)):
                        candidates.append(("EXIT", "range_exit", "触及布林带上轨或止损"))

        action, rule, explanation = min(candidates, key=lambda item: ACTION_PRECEDENCE[item[0]])
        target = _target(action, max_strategy_weight)
        if action != "HOLD":
            captured_breakout = (
                _prior_high([item.high for item in history], 20)
                if strategy_id == "breakout_momentum/v1" and action == "BUY"
                else None
            )
            pending = (action, atr14, captured_breakout)

        signals.append(StrategySignal(
            decision_date=bar.date,
            earliest_execution_date=bars[index + 1].date if index + 1 < len(bars) else None,
            action=action,
            target_weight=target,
            rule=rule,
            explanation=explanation,
            data_cutoff=bar.date,
        ))
    return signals
