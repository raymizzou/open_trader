import pytest

from open_trader.watchlist import ParsedTrigger, parse_watch_trigger


@pytest.mark.parametrize(
    ("text", "trigger_type", "operator", "price"),
    [
        ("below 95", "price", "<=", "95"),
        ("under 95.50", "price", "<=", "95.50"),
        ("breaks below 95", "price", "<=", "95"),
        ("<= 95", "price", "<=", "95"),
        ("above 110", "price", ">=", "110"),
        ("over 110.25", "price", ">=", "110.25"),
        ("breaks above 110", "price", ">=", "110"),
        (">= 110", "price", ">=", "110"),
        ("open below 95", "open_price", "<=", "95"),
        ("open above 110", "open_price", ">=", "110"),
    ],
)
def test_parse_watch_trigger_returns_monitorable_price_trigger(
    text: str,
    trigger_type: str,
    operator: str,
    price: str,
) -> None:
    assert parse_watch_trigger(text) == ParsedTrigger(
        trigger_type=trigger_type,
        operator=operator,
        trigger_price=price,
        trigger_text=text,
        status="active",
        error="",
    )


def test_parse_watch_trigger_marks_empty_trigger_as_no_trigger() -> None:
    assert parse_watch_trigger("") == ParsedTrigger(
        trigger_type="none",
        operator="",
        trigger_price="",
        trigger_text="",
        status="no_trigger",
        error="",
    )


def test_parse_watch_trigger_marks_unclear_text_as_manual_review() -> None:
    text = "watch if support fails"

    assert parse_watch_trigger(text) == ParsedTrigger(
        trigger_type="manual_review",
        operator="",
        trigger_price="",
        trigger_text=text,
        status="manual_review",
        error="",
    )


@pytest.mark.parametrize(
    "text",
    [
        "not below 95",
        "do not buy below 95",
        "below 95 or above 110",
        "above 110 below 95",
        "below 95abc",
    ],
)
def test_parse_watch_trigger_marks_ambiguous_comparisons_as_manual_review(
    text: str,
) -> None:
    assert parse_watch_trigger(text) == ParsedTrigger(
        trigger_type="manual_review",
        operator="",
        trigger_price="",
        trigger_text=text,
        status="manual_review",
        error="",
    )
