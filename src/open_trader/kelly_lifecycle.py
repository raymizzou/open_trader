from __future__ import annotations

from typing import Any

from .kelly_rules import evaluate_kelly_rules


LifecycleState = dict[str, Any]


def build_kelly_lifecycle_states(
    experiment: dict[str, Any],
    *,
    generated_at: str = "",
) -> list[LifecycleState]:
    item = experiment if isinstance(experiment, dict) else {}
    template = item.get("template") if isinstance(item.get("template"), dict) else {}
    rules = template.get("rules") if isinstance(template.get("rules"), dict) else {}
    participants = item.get("participants") if isinstance(item.get("participants"), list) else []

    states: list[LifecycleState] = []
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        facts = _facts_for_participant(item, participant)
        states.append(
            decide_kelly_lifecycle_state(
                participant,
                rules,
                facts,
                generated_at=generated_at,
            ),
        )
    return states


def decide_kelly_lifecycle_state(
    participant: dict[str, Any],
    rules: dict[str, Any],
    facts: dict[str, Any],
    *,
    generated_at: str = "",
) -> LifecycleState:
    item = participant if isinstance(participant, dict) else {}
    normalized_facts = facts if isinstance(facts, dict) else {}
    base = {
        "market": _text(item.get("market")),
        "symbol": _text(item.get("symbol")),
        "updated_at": _text(normalized_facts.get("updated_at") or generated_at),
    }

    execution_error = _text(
        normalized_facts.get("execution_error")
        or normalized_facts.get("execution_failed")
        or normalized_facts.get("order_error"),
    )
    if execution_error:
        return {
            **base,
            "status": "execution_failed",
            "reason": execution_error,
            "action": "停止自动推进，等待人工检查",
        }

    if _truthy(normalized_facts.get("trade_completed")) or _text(normalized_facts.get("trade_status")) == "completed":
        return {
            **base,
            "status": "completed",
            "reason": _text(normalized_facts.get("completed_reason")) or "交易样本已闭环。",
            "action": "计入胜率和盈亏比统计",
        }

    rule_results = evaluate_kelly_rules(rules, normalized_facts)
    if _has_open_position(normalized_facts):
        exit_reason = _first_exit_reason(rule_results)
        if exit_reason:
            return {
                **base,
                "status": "pending_exit_order",
                "reason": f"退出规则触发：{exit_reason}",
                "action": "准备提交模拟盘卖出订单",
            }
        return {
            **base,
            "status": "holding",
            "reason": "模拟盘买入已成交，当前未触发退出规则。",
            "action": "继续检查止盈、止损、移动止盈、时间退出",
        }

    entry = rule_results.get("entry") if isinstance(rule_results.get("entry"), dict) else {}
    if entry.get("triggered") is True:
        if normalized_facts.get("risk_allowed") is False or _truthy(normalized_facts.get("risk_blocked")):
            return {
                **base,
                "status": "risk_blocked",
                "reason": "入场规则触发，但风控未通过。",
                "action": _text(normalized_facts.get("risk_reason")) or "不下单，只记录拦截事件",
            }
        return {
            **base,
            "status": "pending_entry_order",
            "reason": "入场规则触发，Kelly 仓位已计算，风控通过。",
            "action": "准备提交模拟盘买入订单",
        }

    return {
        **base,
        "status": "watching",
        "reason": "等待该策略下一次入场信号。",
        "action": "持续检查入场规则。",
    }


def _facts_for_participant(
    experiment: dict[str, Any],
    participant: dict[str, Any],
) -> dict[str, Any]:
    participant_facts = participant.get("facts")
    if isinstance(participant_facts, dict):
        return participant_facts

    market = _text(participant.get("market")).upper()
    symbol = _text(participant.get("symbol")).upper()
    keys = [f"{market}.{symbol}", symbol]
    symbol_facts = experiment.get("symbol_facts")
    if isinstance(symbol_facts, dict):
        for key in keys:
            facts = symbol_facts.get(key)
            if isinstance(facts, dict):
                return facts
    return {}


def _first_exit_reason(rule_results: dict[str, Any]) -> str:
    for key in ("stop_loss", "take_profit", "trailing_stop", "time_exit"):
        result = rule_results.get(key)
        if not isinstance(result, dict) or result.get("triggered") is not True:
            continue
        reasons = result.get("reasons")
        if isinstance(reasons, list) and reasons:
            return _text(reasons[0])
    return ""


def _has_open_position(facts: dict[str, Any]) -> bool:
    if _truthy(facts.get("has_open_position")) or _truthy(facts.get("position_open")):
        return True
    quantity = _number(facts.get("position_qty") or facts.get("quantity"))
    return quantity is not None and quantity > 0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "open", "completed"}
    return bool(value)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
