from __future__ import annotations

from typing import Any


RuleResult = dict[str, Any]


def evaluate_kelly_rules(rules: dict[str, Any], facts: dict[str, Any]) -> dict[str, RuleResult]:
    """Evaluate structured Kelly strategy rules against prepared market facts."""

    normalized_rules = rules if isinstance(rules, dict) else {}
    normalized_facts = facts if isinstance(facts, dict) else {}
    return {
        "entry": _evaluate_entry(normalized_rules.get("entry"), normalized_facts),
        "stop_loss": _evaluate_stop_loss(normalized_rules.get("stop_loss"), normalized_facts),
        "take_profit": _evaluate_take_profit(normalized_rules.get("take_profit"), normalized_facts),
        "trailing_stop": _evaluate_trailing_stop(normalized_rules.get("trailing_stop"), normalized_facts),
        "time_exit": _evaluate_time_exit(normalized_rules.get("time_exit"), normalized_facts),
    }


def _empty_result(action: dict[str, Any] | None = None) -> RuleResult:
    return {
        "triggered": False,
        "reasons": [],
        "action": action or {},
    }


def _triggered_result(reasons: list[str], action: dict[str, Any]) -> RuleResult:
    return {
        "triggered": bool(reasons),
        "reasons": reasons,
        "action": action if reasons else {},
    }


def _evaluate_entry(rule: Any, facts: dict[str, Any]) -> RuleResult:
    item = rule if isinstance(rule, dict) else {}
    if item.get("type") == "volume_breakout_high":
        return _evaluate_volume_breakout_entry(item, facts)
    if item.get("type") != "pullback_to_moving_average":
        return _empty_result()

    price = _number(facts.get("price"))
    ma_days = item.get("ma_days")
    ma = _fact_by_days(facts.get("moving_averages"), ma_days)
    tolerance_pct = _number(item.get("tolerance_pct"), default=0)
    if price is None or ma is None:
        return _empty_result()
    lower = ma * (1 - tolerance_pct / 100)
    upper = ma * (1 + tolerance_pct / 100)
    if not lower <= price <= upper:
        return _empty_result()

    trend_filter = item.get("trend_filter")
    if isinstance(trend_filter, dict) and trend_filter.get("type") == "moving_average_slope":
        slope = _fact_by_days(facts.get("moving_average_slopes"), trend_filter.get("ma_days"))
        expected = trend_filter.get("direction")
        if slope != expected:
            return _empty_result()
        return _triggered_result(
            [
                f"price {_fmt(price)} is within {_fmt(tolerance_pct)}% of "
                f"MA{ma_days} {_fmt(ma)}; MA{trend_filter.get('ma_days')} slope is {slope}",
            ],
            {"enter": True},
        )

    return _triggered_result(
        [f"price {_fmt(price)} is within {_fmt(tolerance_pct)}% of MA{ma_days} {_fmt(ma)}"],
        {"enter": True},
    )


def _evaluate_volume_breakout_entry(rule: dict[str, Any], facts: dict[str, Any]) -> RuleResult:
    price = _number(facts.get("price"))
    lookback_days = rule.get("lookback_days")
    recent_high = _fact_by_days(facts.get("recent_highs"), lookback_days)
    required_volume = _number(rule.get("volume_multiple"))
    actual_volume = _number(facts.get("volume_multiple"))
    if None in {price, recent_high, required_volume, actual_volume}:
        return _empty_result()
    if price <= recent_high or actual_volume < required_volume:
        return _empty_result()
    return _triggered_result(
        [
            f"price {_fmt(price)} broke above recent {lookback_days}-day high "
            f"{_fmt(recent_high)} with volume multiple {_fmt(actual_volume)}",
        ],
        {"enter": True},
    )


def _evaluate_stop_loss(rule: Any, facts: dict[str, Any]) -> RuleResult:
    item = rule if isinstance(rule, dict) else {}
    if item.get("type") not in {"any_of", "min_of"}:
        return _empty_result()

    reasons: list[str] = []
    for child in item.get("rules", []):
        if not isinstance(child, dict):
            continue
        reason = _evaluate_stop_loss_child(child, facts)
        if reason:
            reasons.append(reason)
    return _triggered_result(reasons, {"exit_pct": 100})


def _evaluate_stop_loss_child(rule: dict[str, Any], facts: dict[str, Any]) -> str:
    close_price = _number(facts.get("close_price"))
    if close_price is None:
        return ""

    if rule.get("type") == "pct_below_moving_average":
        ma_days = rule.get("ma_days")
        ma = _fact_by_days(facts.get("moving_averages"), ma_days)
        pct = _number(rule.get("pct"), default=0)
        if ma is None:
            return ""
        threshold = ma * (1 - pct / 100)
        if close_price <= threshold:
            return f"close {_fmt(close_price)} is below MA{ma_days} {_fmt(ma)} by at least {_fmt(pct)}%"
        return ""

    if rule.get("type") == "recent_swing_low_break":
        lookback_days = rule.get("lookback_days")
        swing_low = _fact_by_days(facts.get("recent_swing_lows"), lookback_days)
        if swing_low is not None and close_price < swing_low:
            return f"close {_fmt(close_price)} is below recent {lookback_days}-day swing low {_fmt(swing_low)}"
        return ""

    if rule.get("type") == "pct_below_reference_price":
        reference = rule.get("reference")
        reference_price = _number(facts.get(str(reference)))
        pct = _number(rule.get("pct"), default=0)
        if reference_price is None:
            return ""
        threshold = reference_price * (1 - pct / 100)
        if close_price <= threshold:
            return (
                f"close {_fmt(close_price)} is below {reference} "
                f"{_fmt(reference_price)} by at least {_fmt(pct)}%"
            )
        return ""

    if rule.get("type") == "atr_below_entry":
        entry_price = _number(facts.get("entry_price"))
        atr = _number(facts.get("atr"))
        atr_multiple = _number(rule.get("atr_multiple"))
        if None in {entry_price, atr, atr_multiple}:
            return ""
        threshold = entry_price - atr_multiple * atr
        if close_price <= threshold:
            return f"close {_fmt(close_price)} is below entry {_fmt(entry_price)} - {_fmt(atr_multiple)} ATR"
        return ""

    return ""


def _evaluate_take_profit(rule: Any, facts: dict[str, Any]) -> RuleResult:
    item = rule if isinstance(rule, dict) else {}
    if item.get("type") != "risk_multiple":
        return _empty_result()

    price = _number(facts.get("price"))
    entry_price = _number(facts.get("entry_price"))
    initial_risk = _number(facts.get("initial_risk_per_share"))
    trigger_r = _number(item.get("trigger_r"))
    if None in {price, entry_price, initial_risk, trigger_r}:
        return _empty_result()
    target_price = entry_price + trigger_r * initial_risk
    if price < target_price:
        return _empty_result()
    return _triggered_result(
        [f"price {_fmt(price)} reached entry {_fmt(entry_price)} + {_fmt(trigger_r)}R"],
        {"sell_pct": item.get("sell_pct")},
    )


def _evaluate_trailing_stop(rule: Any, facts: dict[str, Any]) -> RuleResult:
    item = rule if isinstance(rule, dict) else {}
    if item.get("type") == "close_below_recent_low":
        close_price = _number(facts.get("close_price"))
        lookback_days = item.get("lookback_days")
        recent_low = _fact_by_days(facts.get("recent_lows"), lookback_days)
        if close_price is None or recent_low is None or close_price >= recent_low:
            return _empty_result()
        return _triggered_result(
            [f"close {_fmt(close_price)} is below recent {lookback_days}-day low {_fmt(recent_low)}"],
            {"exit_remaining": bool(item.get("apply_to_remaining_position"))},
        )
    if item.get("type") != "close_below_moving_average":
        return _empty_result()

    close_price = _number(facts.get("close_price"))
    ma_days = item.get("ma_days")
    ma = _fact_by_days(facts.get("moving_averages"), ma_days)
    if close_price is None or ma is None or close_price >= ma:
        return _empty_result()
    return _triggered_result(
        [f"close {_fmt(close_price)} is below MA{ma_days} {_fmt(ma)}"],
        {"exit_remaining": bool(item.get("apply_to_remaining_position"))},
    )


def _evaluate_time_exit(rule: Any, facts: dict[str, Any]) -> RuleResult:
    item = rule if isinstance(rule, dict) else {}
    if item.get("type") != "max_holding_days":
        return _empty_result()

    holding_days = _number(facts.get("holding_days"))
    max_days = _number(item.get("days"))
    if holding_days is None or max_days is None or holding_days < max_days:
        return _empty_result()

    if item.get("exit_if") == "no_take_profit_or_stop_loss":
        if facts.get("take_profit_triggered") or facts.get("stop_loss_triggered"):
            return _empty_result()
        return _triggered_result(
            [
                f"holding days {_fmt(holding_days)} reached max {_fmt(max_days)} "
                "without take-profit or stop-loss",
            ],
            {"exit_pct": 100},
        )

    if item.get("exit_if") == "minimum_unrealized_r_not_reached":
        min_unrealized_r = _number(item.get("min_unrealized_r"))
        unrealized_r = _number(facts.get("unrealized_r"))
        if min_unrealized_r is None or unrealized_r is None or unrealized_r >= min_unrealized_r:
            return _empty_result()
        return _triggered_result(
            [
                f"holding days {_fmt(holding_days)} reached max {_fmt(max_days)} "
                f"without reaching {_fmt(min_unrealized_r)}R unrealized profit",
            ],
            {"exit_pct": 100},
        )

    return _triggered_result(
        [f"holding days {_fmt(holding_days)} reached max {_fmt(max_days)}"],
        {"exit_pct": 100},
    )


def _fact_by_days(values: Any, days: Any) -> Any:
    if not isinstance(values, dict):
        return None
    if days in values:
        return values[days]
    return values.get(str(days))


def _number(value: Any, *, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt(value: Any) -> str:
    number = _number(value)
    if number is None:
        return str(value)
    if number.is_integer():
        return str(int(number))
    return str(number)
