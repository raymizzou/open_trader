from __future__ import annotations

from open_trader.advice.trader_template import format_trader_template


def test_format_trader_template_returns_raw_text_for_unstructured_decision() -> None:
    text = "Hold the position until the next earnings report."

    assert format_trader_template(text, "Hold") == text


def test_format_trader_template_uses_action_when_rating_is_missing() -> None:
    formatted = format_trader_template(
        (
            "**Executive Summary**: 维持当前仓位。若跌破340美元则止损。\n\n"
            "**Investment Thesis**: 基本面稳定。\n\n"
            "**Price Target**: 450\n\n"
            "**Time Horizon**: 3个月"
        ),
        "Hold",
    )

    assert formatted.splitlines()[0] == "评级：Hold"
    assert "风控：若跌破340美元则止损。" in formatted
