from __future__ import annotations

from open_trader.kelly_rules import evaluate_kelly_rules


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


def test_evaluate_kelly_rules_triggers_entry_from_structured_pullback_rule() -> None:
    result = evaluate_kelly_rules(
        trend_pullback_rules(),
        {
            "price": 99.4,
            "moving_averages": {"20": 100},
            "moving_average_slopes": {"50": "up"},
        },
    )

    assert result["entry"]["triggered"] is True
    assert result["entry"]["action"] == {"enter": True}
    assert result["entry"]["reasons"] == [
        "price 99.4 is within 1% of MA20 100; MA50 slope is up",
    ]


def test_evaluate_kelly_rules_blocks_entry_when_trend_filter_fails() -> None:
    result = evaluate_kelly_rules(
        trend_pullback_rules(),
        {
            "price": 99.4,
            "moving_averages": {"20": 100},
            "moving_average_slopes": {"50": "down"},
        },
    )

    assert result["entry"]["triggered"] is False
    assert result["entry"]["reasons"] == []


def test_evaluate_kelly_rules_triggers_exit_rules_from_trade_facts() -> None:
    result = evaluate_kelly_rules(
        trend_pullback_rules(),
        {
            "price": 104,
            "close_price": 96.8,
            "entry_price": 100,
            "initial_risk_per_share": 2,
            "moving_averages": {"10": 98, "20": 100},
            "recent_swing_lows": {"20": 97},
            "holding_days": 20,
            "take_profit_triggered": False,
            "stop_loss_triggered": False,
        },
    )

    assert result["stop_loss"]["triggered"] is True
    assert result["stop_loss"]["action"] == {"exit_pct": 100}
    assert result["stop_loss"]["reasons"] == [
        "close 96.8 is below MA20 100 by at least 3%",
        "close 96.8 is below recent 20-day swing low 97",
    ]

    assert result["take_profit"]["triggered"] is True
    assert result["take_profit"]["action"] == {"sell_pct": 50}
    assert result["take_profit"]["reasons"] == [
        "price 104 reached entry 100 + 2R",
    ]

    assert result["trailing_stop"]["triggered"] is True
    assert result["trailing_stop"]["action"] == {"exit_remaining": True}
    assert result["trailing_stop"]["reasons"] == [
        "close 96.8 is below MA10 98",
    ]

    assert result["time_exit"]["triggered"] is True
    assert result["time_exit"]["action"] == {"exit_pct": 100}
    assert result["time_exit"]["reasons"] == [
        "holding days 20 reached max 20 without take-profit or stop-loss",
    ]
