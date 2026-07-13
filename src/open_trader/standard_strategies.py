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


def build_current_strategy_snapshot(
    strategy_id: str,
    bars: Sequence[StrategyBar],
    max_strategy_weight: Decimal,
) -> dict[str, object]:
    if strategy_id not in {item.strategy_id for item in _CATALOG}:
        raise ValueError(f"未知策略：{strategy_id}")
    if not bars:
        raise ValueError("策略快照需要日线数据")
    if max_strategy_weight < 0:
        raise ValueError("最大策略权重不能为负数")

    definition = next(item for item in _CATALOG if item.strategy_id == strategy_id)
    source_date = bars[-1].date
    closes = [bar.close for bar in bars]
    sma10, sma20, sma50 = _sma(closes, 10), _sma(closes, 20), _sma(closes, 50)
    atr14, rsi14 = _atr(bars, 14), _rsi(closes, 14)
    bands = _bollinger(closes, 20, Decimal("2"))
    lower, middle, upper = bands or (None, None, None)
    prior5 = _prior_high(closes, 5)
    prior20 = _prior_high([bar.high for bar in bars], 20)
    prior_volume = _sma([bar.volume for bar in bars[:-1]], 20)
    relative_volume = (
        bars[-1].volume / prior_volume
        if prior_volume not in (None, Decimal("0"))
        else None
    )
    ma20_distance = (
        (bars[-1].close / sma20 - Decimal("1")) * Decimal("100")
        if sma20 not in (None, Decimal("0"))
        else None
    )
    position = (
        "below_lower" if lower is not None and bars[-1].close < lower
        else "above_upper" if upper is not None and bars[-1].close > upper
        else "inside"
    )
    state = _replay_strategy_state(strategy_id, bars, max_strategy_weight)
    facts = {
        "close": _fact(bars[-1].close, "latest completed close", {}, source_date),
        "sma10": _fact(sma10, "SMA(close, 10)", {"period": 10}, source_date),
        "sma20": _fact(sma20, "SMA(close, 20)", {"period": 20}, source_date),
        "sma50": _fact(sma50, "SMA(close, 50)", {"period": 50}, source_date),
        "ma20_distance_pct": _fact(
            ma20_distance, "(close / sma20 - 1) * 100", {"close": bars[-1].close, "sma20": sma20}, source_date,
        ),
        "atr14": _fact(atr14, "ATR(high, low, close, 14)", {"period": 14}, source_date),
        "rsi14": _fact(rsi14, "Wilder RSI(close, 14)", {"period": 14}, source_date),
        "bollinger_lower": _fact(lower, "sma20 - 2 * stddev(close, 20)", {"period": 20, "multiplier": 2}, source_date),
        "bollinger_middle": _fact(middle, "SMA(close, 20)", {"period": 20}, source_date),
        "bollinger_upper": _fact(upper, "sma20 + 2 * stddev(close, 20)", {"period": 20, "multiplier": 2}, source_date),
        "bollinger_position": _fact(position, "compare(close, bollinger bands)", {"close": bars[-1].close}, source_date),
        "relative_volume": _fact(
            relative_volume, "volume / SMA(previous volume, 20)",
            {"volume": bars[-1].volume, "average_volume": prior_volume}, source_date,
        ),
    }
    conditions: list[dict[str, object]] = []

    def add(
        condition_id: str, priority: str, operator: str, value: Decimal | None,
        target_weight: Decimal, action: str, formula: str, inputs: Mapping[str, object],
    ) -> None:
        if value is None:
            return
        conditions.append({
            "condition_id": condition_id,
            "priority": priority,
            "operator": operator,
            "calculated_value": str(value),
            "target_weight": str(target_weight),
            "suggested_action": action,
            "formula": formula,
            "inputs": {key: str(item) for key, item in inputs.items()},
            "source_date": source_date.isoformat(),
        })

    half = max_strategy_weight * Decimal("0.5")
    active_stop = state["active_stop"]
    if strategy_id == "trend_pullback/v1":
        protection = min(sma50, active_stop) if sma50 is not None and active_stop is not None else sma50 or active_stop
        add("trend-exit", "risk", "<=", protection, Decimal("0"), "退出",
            "min(sma50, active_stop)", {"sma50": sma50, "active_stop": active_stop})
        add("trend-reduce", "ordinary", ">=", sma20 + Decimal("2") * atr14 if sma20 is not None and atr14 is not None else None,
            half, "减仓", "sma20 + 2 * atr14", {"sma20": sma20, "atr14": atr14})
        add("trend-add", "ordinary", ">=", prior5, max_strategy_weight, "加仓",
            "max(previous 5 closes)", {"prior5": prior5})
        add("trend-buy", "ordinary", ">=", sma20, half, "建立观察仓",
            "SMA(close, 20)", {"sma20": sma20})
    elif strategy_id == "breakout_momentum/v1":
        breakout = state["breakout_level"] or prior20
        protection = min(breakout, active_stop) if breakout is not None and active_stop is not None else breakout or active_stop
        add("breakout-exit", "risk", "<=", protection, Decimal("0"), "退出",
            "min(breakout_level, active_stop)", {"breakout_level": breakout, "active_stop": active_stop})
        add("breakout-reduce", "ordinary", "<=", sma10, half, "减仓",
            "SMA(close, 10)", {"sma10": sma10})
        add("breakout-add", "ordinary", ">=", state["entry_price"] + atr14 if state["entry_price"] is not None and atr14 is not None else None,
            max_strategy_weight, "加仓", "entry_price + atr14", {"entry_price": state["entry_price"], "atr14": atr14})
        add("breakout-buy", "ordinary", ">=", prior20, half, "建立观察仓",
            "max(previous 20 highs)", {"prior20_high": prior20, "relative_volume_required": "1.5"})
    else:
        add("range-stop", "risk", "<=", active_stop, Decimal("0"), "止损退出",
            "active_stop", {"active_stop": active_stop})
        add("range-exit", "risk", ">=", upper, Decimal("0"), "止盈退出",
            "bollinger_upper", {"bollinger_upper": upper})
        add("range-reduce", "ordinary", ">=", middle, half, "减仓",
            "bollinger_middle", {"bollinger_middle": middle})
        add("range-add", "ordinary", ">=", lower, max_strategy_weight, "加仓",
            "bollinger_lower", {"bollinger_lower": lower})
        add("range-buy", "ordinary", "<=", lower, half, "建立观察仓",
            "bollinger_lower and rsi14 <= 30", {"bollinger_lower": lower, "rsi14_required": 30})

    return {
        "strategy": definition.to_dict(),
        "facts": facts,
        "conditions": conditions,
    }


def _fact(
    value: object,
    formula: str,
    inputs: Mapping[str, object],
    source_date: date,
) -> dict[str, object]:
    return {
        "formula": formula,
        "inputs": {key: str(item) for key, item in inputs.items()},
        "source_date": source_date.isoformat(),
        "calculated_value": None if value is None else str(value),
    }


def _replay_strategy_state(
    strategy_id: str,
    bars: Sequence[StrategyBar],
    max_strategy_weight: Decimal,
) -> dict[str, Decimal | None]:
    signals = generate_strategy_signals(
        strategy_id, bars, start_date=bars[0].date,
        max_strategy_weight=max_strategy_weight,
    )
    bar_by_date = {bar.date: bar for bar in bars}
    index_by_date = {bar.date: index for index, bar in enumerate(bars)}
    active_stop: Decimal | None = None
    entry_price: Decimal | None = None
    breakout_level: Decimal | None = None
    for signal in signals:
        execution = bar_by_date.get(signal.earliest_execution_date)
        if execution is None or signal.action == "HOLD":
            continue
        history = bars[: index_by_date[signal.decision_date] + 1]
        atr14 = _atr(history, 14)
        if signal.action == "BUY":
            entry_price = execution.open
            if atr14 is not None:
                active_stop = execution.open - Decimal("2") * atr14
            if strategy_id == "breakout_momentum/v1":
                breakout_level = _prior_high([bar.high for bar in history], 20)
        elif signal.action == "ADD" and atr14 is not None and strategy_id != "range_mean_reversion/v1":
            active_stop = execution.open - Decimal("2") * atr14
        elif signal.action == "EXIT":
            active_stop = entry_price = breakout_level = None
    return {
        "active_stop": active_stop,
        "entry_price": entry_price,
        "breakout_level": breakout_level,
    }
