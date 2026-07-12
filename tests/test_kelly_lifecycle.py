from __future__ import annotations

from open_trader.kelly_lifecycle import build_kelly_lifecycle_states, decide_kelly_lifecycle_state


def trend_pullback_rules() -> dict[str, object]:
    return {
        "entry": {
            "type": "pullback_to_moving_average",
            "ma_days": 20,
            "tolerance_pct": 1,
            "trend_filter": {
                "type": "moving_average_slope",
                "ma_days": 50,
                "direction": "up",
            },
        },
        "stop_loss": {
            "type": "any_of",
            "rules": [
                {"type": "pct_below_moving_average", "ma_days": 20, "pct": 3},
                {"type": "recent_swing_low_break", "lookback_days": 20},
            ],
        },
        "take_profit": {"type": "risk_multiple", "trigger_r": 2, "sell_pct": 50},
        "trailing_stop": {
            "type": "close_below_moving_average",
            "ma_days": 10,
            "apply_to_remaining_position": True,
        },
        "time_exit": {
            "type": "max_holding_days",
            "days": 20,
            "exit_if": "no_take_profit_or_stop_loss",
        },
    }


def test_decide_lifecycle_state_returns_watching_when_entry_rule_is_not_triggered() -> None:
    state = decide_kelly_lifecycle_state(
        {"market": "US", "symbol": "AAPL"},
        trend_pullback_rules(),
        {
            "price": 104,
            "moving_averages": {"20": 100},
            "moving_average_slopes": {"50": "up"},
            "updated_at": "2026-07-08 10:00",
        },
    )

    assert state == {
        "market": "US",
        "symbol": "AAPL",
        "status": "watching",
        "reason": "等待该策略下一次入场信号。",
        "action": "持续检查入场规则。",
        "updated_at": "2026-07-08 10:00",
    }


def test_decide_lifecycle_state_moves_to_pending_entry_after_entry_trigger() -> None:
    state = decide_kelly_lifecycle_state(
        {"market": "US", "symbol": "AAPL"},
        trend_pullback_rules(),
        {
            "price": 99.4,
            "moving_averages": {"20": 100},
            "moving_average_slopes": {"50": "up"},
            "risk_allowed": True,
        },
    )

    assert state["status"] == "pending_entry_order"
    assert state["reason"] == "入场规则触发，仓位计算与风控检查待执行。"
    assert state["action"] == "等待仓位计算与风控检查"
    assert "4%" not in f"{state['reason']} {state['action']}"
    assert "风控通过" not in f"{state['reason']} {state['action']}"


def test_decide_lifecycle_state_marks_risk_blocked_after_entry_trigger() -> None:
    state = decide_kelly_lifecycle_state(
        {"market": "US", "symbol": "AAPL"},
        trend_pullback_rules(),
        {
            "price": 99.4,
            "moving_averages": {"20": 100},
            "moving_average_slopes": {"50": "up"},
            "risk_allowed": False,
            "risk_reason": "策略总仓位上限已满。",
        },
    )

    assert state["status"] == "risk_blocked"
    assert state["reason"] == "入场规则触发，但风控未通过。"
    assert state["action"] == "策略总仓位上限已满。"


def test_decide_lifecycle_state_keeps_open_position_holding_until_exit_triggers() -> None:
    state = decide_kelly_lifecycle_state(
        {"market": "US", "symbol": "AAPL"},
        trend_pullback_rules(),
        {
            "has_open_position": True,
            "price": 101,
            "close_price": 101,
            "entry_price": 100,
            "initial_risk_per_share": 2,
            "moving_averages": {"10": 98, "20": 100},
            "recent_swing_lows": {"20": 97},
            "holding_days": 5,
        },
    )

    assert state["status"] == "holding"
    assert state["reason"] == "模拟盘买入已成交，当前未触发退出规则。"
    assert state["action"] == "继续检查止盈、止损、移动止盈、时间退出"


def test_decide_lifecycle_state_moves_open_position_to_pending_exit_when_exit_triggers() -> None:
    state = decide_kelly_lifecycle_state(
        {"market": "US", "symbol": "AAPL"},
        trend_pullback_rules(),
        {
            "has_open_position": True,
            "price": 104,
            "close_price": 104,
            "entry_price": 100,
            "initial_risk_per_share": 2,
            "moving_averages": {"10": 98, "20": 100},
            "recent_swing_lows": {"20": 97},
            "holding_days": 5,
        },
    )

    assert state["status"] == "pending_exit_order"
    assert state["reason"] == "退出规则触发：price 104 reached entry 100 + 2R"
    assert state["action"] == "准备提交模拟盘卖出订单"


def test_decide_lifecycle_state_gives_terminal_and_error_states_priority() -> None:
    completed = decide_kelly_lifecycle_state(
        {"market": "US", "symbol": "AAPL"},
        trend_pullback_rules(),
        {"trade_completed": True},
    )
    failed = decide_kelly_lifecycle_state(
        {"market": "US", "symbol": "AAPL"},
        trend_pullback_rules(),
        {"execution_error": "模拟盘订单同步失败。", "trade_completed": True},
    )

    assert completed["status"] == "completed"
    assert completed["reason"] == "交易样本已闭环。"
    assert failed["status"] == "execution_failed"
    assert failed["reason"] == "模拟盘订单同步失败。"


def test_build_lifecycle_states_uses_experiment_symbol_facts_per_participant() -> None:
    states = build_kelly_lifecycle_states(
        {
            "template": {"rules": trend_pullback_rules()},
            "participants": [
                {"market": "US", "symbol": "AAPL", "name": "Apple", "source": "holding"},
                {"market": "US", "symbol": "MSFT", "name": "Microsoft", "source": "watchlist"},
            ],
            "symbol_facts": {
                "US.AAPL": {
                    "price": 99.4,
                    "moving_averages": {"20": 100},
                    "moving_average_slopes": {"50": "up"},
                },
                "US.MSFT": {
                    "has_open_position": True,
                    "price": 104,
                    "close_price": 104,
                    "entry_price": 100,
                    "initial_risk_per_share": 2,
                    "moving_averages": {"10": 98, "20": 100},
                    "recent_swing_lows": {"20": 97},
                },
            },
        },
        generated_at="2026-07-08 12:00",
    )

    assert [state["symbol"] for state in states] == ["AAPL", "MSFT"]
    assert [state["status"] for state in states] == ["pending_entry_order", "pending_exit_order"]
    assert [state["updated_at"] for state in states] == ["2026-07-08 12:00", "2026-07-08 12:00"]
