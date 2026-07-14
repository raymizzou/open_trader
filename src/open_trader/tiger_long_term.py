from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from typing import Mapping, Sequence

from .standard_strategies import StrategyBar


STRATEGY_ID = "tiger_sma200_equal_weight/v1"
SYMBOL_CAP = Decimal("0.10")
RISK_GROUP_CAP = Decimal("0.30")
DRIFT_TOLERANCE = Decimal("0.02")


@dataclass(frozen=True)
class TigerLongTermConfig:
    strategy_id: str
    account_alias: str
    members: Mapping[str, str]


def load_tiger_long_term_config(path: Path) -> TigerLongTermConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Tiger 长线策略配置无效") from exc
    strategy_id = str(payload.get("strategy_id") or "") if isinstance(payload, dict) else ""
    account_alias = str(payload.get("account_alias") or "") if isinstance(payload, dict) else ""
    members = payload.get("members") if isinstance(payload, dict) else None
    if (
        strategy_id != STRATEGY_ID
        or not account_alias
        or not isinstance(members, dict)
        or not members
    ):
        raise ValueError("Tiger 长线策略配置无效")
    normalized = {
        str(symbol).strip().upper(): str(group).strip()
        for symbol, group in members.items()
    }
    if any(not symbol or not group for symbol, group in normalized.items()):
        raise ValueError("Tiger 长线策略配置无效")
    return TigerLongTermConfig(strategy_id, account_alias, normalized)


def sma200_state(bars: Sequence[StrategyBar]) -> str:
    if len(bars) < 201:
        return "INELIGIBLE"
    sma200 = sum((bar.close for bar in bars[-201:-1]), Decimal("0")) / Decimal(200)
    return "LONG" if bars[-1].close > sma200 else "CASH"


def allocate_target_weights(
    states: Mapping[str, str],
    risk_groups: Mapping[str, str],
) -> dict[str, Decimal]:
    long_symbols = [symbol for symbol, state in states.items() if state == "LONG"]
    if not long_symbols:
        return {symbol: Decimal("0") for symbol in states}
    if any(symbol not in risk_groups for symbol in long_symbols):
        raise ValueError("Tiger 长线策略成员缺少风险组")
    weight = min(SYMBOL_CAP, Decimal("1") / Decimal(len(long_symbols)))
    targets = {
        symbol: weight if symbol in long_symbols else Decimal("0")
        for symbol in states
    }
    by_group: dict[str, list[str]] = {}
    for symbol in long_symbols:
        by_group.setdefault(risk_groups[symbol], []).append(symbol)
    for symbols in by_group.values():
        total = sum((targets[symbol] for symbol in symbols), Decimal("0"))
        if total <= RISK_GROUP_CAP:
            continue
        scale = RISK_GROUP_CAP / total
        for symbol in symbols:
            targets[symbol] *= scale
    return targets


def rebalance_reasons(
    actual: Mapping[str, Decimal],
    target: Mapping[str, Decimal],
    previous_states: Mapping[str, str],
    states: Mapping[str, str],
    risk_groups: Mapping[str, str],
) -> dict[str, str]:
    symbols = set(actual) | set(target) | set(states)
    group_weights: dict[str, Decimal] = {}
    for symbol, weight in actual.items():
        group = risk_groups.get(symbol)
        if group:
            group_weights[group] = group_weights.get(group, Decimal("0")) + weight
    reasons: dict[str, str] = {}
    for symbol in symbols:
        current = actual.get(symbol, Decimal("0"))
        desired = target.get(symbol, Decimal("0"))
        if previous_states.get(symbol) != states.get(symbol):
            reasons[symbol] = "state_change"
        elif current > SYMBOL_CAP:
            reasons[symbol] = "symbol_cap"
        elif group_weights.get(risk_groups.get(symbol, ""), Decimal("0")) > RISK_GROUP_CAP:
            reasons[symbol] = "risk_group_cap"
        elif abs(current - desired) > DRIFT_TOLERANCE:
            reasons[symbol] = "drift"
    return reasons
