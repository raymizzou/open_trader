import csv
from pathlib import Path

import pytest

from open_trader.watchlist import ParsedTrigger, build_watchlist, parse_watch_trigger


ACTION_FIELDNAMES = [
    "run_date",
    "symbol",
    "market",
    "portfolio_weight_hkd",
    "severity",
    "change_type",
    "suggested_action",
    "summary",
    "rationale",
    "watch_trigger",
]


def write_actions(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACTION_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


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


def test_build_watchlist_writes_run_and_latest_outputs(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_actions(
        actions_path,
        [
            {
                "run_date": "2026-06-16",
                "symbol": "VIXY",
                "market": "US",
                "portfolio_weight_hkd": "3.05%",
                "severity": "high",
                "change_type": "action_changed",
                "suggested_action": "reduce",
                "summary": "VIXY changed",
                "rationale": "Fake rationale",
                "watch_trigger": "below 95",
            },
            {
                "run_date": "2026-06-16",
                "symbol": "QQQ",
                "market": "US",
                "portfolio_weight_hkd": "1.40%",
                "severity": "medium",
                "change_type": "new_signal",
                "suggested_action": "watch",
                "summary": "QQQ changed",
                "rationale": "Fake rationale",
                "watch_trigger": "support fails",
            },
        ],
    )

    result = build_watchlist(
        actions_path,
        tmp_path / "data",
        run_date=None,
        update_latest=True,
    )

    assert result.run_date == "2026-06-16"
    assert result.watchlist_count == 2
    assert result.watchlist_path == tmp_path / "data/runs/2026-06-16/watchlist.csv"
    assert result.latest_path == tmp_path / "data/latest/watchlist.csv"
    assert result.watchlist_path.exists()
    assert result.latest_path.exists()

    rows = list(csv.DictReader(result.watchlist_path.open(encoding="utf-8")))
    assert rows[0]["symbol"] == "VIXY"
    assert rows[0]["trigger_type"] == "price"
    assert rows[0]["operator"] == "<="
    assert rows[0]["trigger_price"] == "95"
    assert rows[0]["status"] == "active"
    assert rows[1]["symbol"] == "QQQ"
    assert rows[1]["trigger_type"] == "manual_review"
    assert rows[1]["status"] == "manual_review"


def test_build_watchlist_dry_run_does_not_update_latest(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    latest_path = tmp_path / "data/latest/watchlist.csv"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text("existing\n", encoding="utf-8")
    write_actions(
        actions_path,
        [
            {
                "run_date": "2026-06-16",
                "symbol": "VIXY",
                "market": "US",
                "portfolio_weight_hkd": "3.05%",
                "severity": "high",
                "change_type": "action_changed",
                "suggested_action": "reduce",
                "summary": "VIXY changed",
                "rationale": "Fake rationale",
                "watch_trigger": "below 95",
            },
        ],
    )

    result = build_watchlist(
        actions_path=actions_path,
        data_dir=tmp_path / "data",
        run_date=None,
        update_latest=False,
    )

    assert result.watchlist_path.exists()
    assert latest_path.read_text(encoding="utf-8") == "existing\n"


def test_build_watchlist_writes_empty_headers_when_no_actions(tmp_path: Path) -> None:
    actions_path = tmp_path / "data/latest/premarket_actions.csv"
    write_actions(actions_path, [])

    result = build_watchlist(
        actions_path=actions_path,
        data_dir=tmp_path / "data",
        run_date="2026-06-16",
        update_latest=True,
    )

    rows = list(csv.DictReader(result.watchlist_path.open(encoding="utf-8")))
    assert rows == []
    assert result.watchlist_count == 0
